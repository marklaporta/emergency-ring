"""The edges of the state machine and of the HTTP layer: a message that
arrives twice, late, out of order, half-formed or from the wrong caller.

Each test pins a behaviour that nothing else would notice going wrong until a
real alert went unanswered. Where the code is meant to log a failure, the test
asserts the log line, so a passing run prints nothing but its dots.
"""
import base64
import importlib.util
import json
import os
import time
import unittest
import urllib.parse
from unittest import mock

from support import (
    ASSISTANT, BACKUP, BASE, DOMAIN, PRIMARY, ROBIN, TWILIO_TOKEN, call, event, make, request,
)

from er import handler
from er.core import SELF_TEST_SENDER, Service
from er.fakes import ACK, NO_ANSWER
from er.models import ACKNOWLEDGED, ACTIVE, CANCELLED, EXPIRED
from er.twilio_adapter import compute_signature

# A second sender, for the cases where one sender meets another's incident.
JO = "jo-test-token-0123456789"

# Three calls to the owner and two to the backup contact, one a minute, the
# first at the same moment as the push.
WITH_BACKUP = dict(
    backup_number=BACKUP, primary_attempts=3, backup_attempts=2,
    arm_offset_seconds=0, cycle_seconds=60,
)


def boom(*args, **kwargs):
    raise RuntimeError("unavailable")


def answered_on(call_number):
    """Nobody picks up until this call, which is answered and acknowledged."""
    return lambda n, to: ACK if n == call_number else NO_ANSWER


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode()


def records(*messages):
    """The event SQS delivers for these queue messages."""
    return {"Records": [{"body": json.dumps(message)} for message in messages]}


def queued(sim):
    """Every tick waiting in the queue, as (due, incident id, seq)."""
    return [(due, incident_id, seq) for due, _, incident_id, seq in sim.scheduler.queue]


def from_twilio(path, fields, key=TWILIO_TOKEN, encoded=False):
    """The event for a webhook from Twilio, signed with `key` for the URL
    Twilio was given."""
    body = urllib.parse.urlencode(fields)
    return event("POST", path, raw=b64(body) if encoded else body, encoded=encoded,
                 headers={"X-Twilio-Signature": compute_signature(key, BASE + path, fields)})


def answer(made):
    """Put an event through the handler; returns (status, body)."""
    response = handler.lambda_handler(made)
    try:
        return response["statusCode"], json.loads(response["body"])
    except ValueError:
        return response["statusCode"], response["body"]


def no_answer_then_tick(sim, incident_id):
    """The ringing call ends unanswered, a cycle passes, and the next tick runs."""
    incident = sim.store.get(incident_id)
    sim.service.call_status(incident_id, incident.last_call_sid, "no-answer")
    sim.clock.t += sim.cfg.cycle_seconds
    return sim.service.tick(incident_id, incident.tick_seq)


class Edge(unittest.TestCase):
    def setUp(self):
        # PUBLIC_BASE_URL changes every URL the service hands out, so each
        # test starts without it and the environment is put back afterwards.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PUBLIC_BASE_URL", None)


# ---------------------------------------------------------------- the queue

class QueueRedelivery(Edge):
    """SQS delivers a message again only if the function raises. A failure
    that is caught and answered with success is a redial that never happens."""

    def test_a_tick_that_fails_is_raised_and_the_redelivery_does_the_work(self):
        for broken in ("scheduler.schedule_tick", "store.advance_tick", "store.get"):
            with self.subTest(broken=broken):
                sim = make(arm_offset_seconds=0)
                incident = sim.trigger()
                sim.service.call_status(incident.id, "CA0001", "no-answer")
                sim.clock.t += sim.cfg.cycle_seconds
                tick = records({"incident_id": incident.id, "seq": 0})
                part, method = broken.split(".")
                with mock.patch.object(getattr(sim, part), method, boom):
                    with self.assertRaises(RuntimeError):
                        handler.lambda_handler(tick)
                # Nothing was committed, so the same message can do it all again.
                self.assertEqual(sim.store.get(incident.id).tick_seq, 0)
                self.assertEqual(len(sim.telephony.calls), 1)
                self.assertEqual(handler.lambda_handler(tick), {"ok": True})
                self.assertEqual(sim.store.get(incident.id).tick_seq, 1)
                self.assertEqual(len(sim.telephony.calls), 2)
                # The failed try may have queued a spare copy of the next
                # tick. It must not add a call.
                sim.run()
                self.assertEqual(len(sim.telephony.calls), sim.cfg.total_attempts)

    def test_a_queued_push_that_fails_is_raised_and_the_redelivery_sends_one(self):
        sim = make(first_arm="call")
        incident = sim.trigger()
        push = records({"incident_id": incident.id, "action": "push"})
        with mock.patch.object(sim.notifier, "send_emergency", boom):
            with self.assertRaises(RuntimeError):
                handler.lambda_handler(push)
        self.assertIsNone(sim.store.get(incident.id).pushover_receipt)
        self.assertEqual(handler.lambda_handler(push), {"ok": True})
        self.assertEqual(handler.lambda_handler(push), {"ok": True})   # and once more, as SQS may
        self.assertEqual(sim.notifier.emergencies, ["rcpt-" + incident.id])

    def test_every_message_in_a_batch_is_handled(self):
        sim = make(first_arm="call")
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "no-answer")
        sim.clock.t += sim.cfg.cycle_seconds
        handler.lambda_handler(records(
            {"incident_id": incident.id, "action": "push"},
            {"incident_id": incident.id, "seq": 0},
        ))
        self.assertEqual((len(sim.notifier.emergencies), len(sim.telephony.calls)), (1, 2))

    def test_a_message_for_an_incident_that_is_gone_is_dropped_not_retried(self):
        make()
        gone = "x" * 16
        self.assertEqual(handler.lambda_handler(records(
            {"incident_id": gone, "seq": 0}, {"incident_id": gone, "action": "push"},
        )), {"ok": True})


class TickOrder(Edge):
    def ready_to_redial(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "no-answer")
        sim.clock.t += sim.cfg.cycle_seconds
        return sim, incident

    def test_the_next_tick_is_queued_before_this_one_is_committed_and_then_it_dials(self):
        # Queue, commit, dial. Commit first and a failed enqueue ends the
        # redials for good; dial first and a duplicate delivery dials twice.
        sim, incident = self.ready_to_redial()
        order = []

        def noting(step, real):
            def wrapper(*args, **kwargs):
                order.append(step)
                return real(*args, **kwargs)
            return wrapper

        with mock.patch.object(sim.scheduler, "schedule_tick",
                               noting("queue", sim.scheduler.schedule_tick)), \
                mock.patch.object(sim.store, "advance_tick",
                                  noting("commit", sim.store.advance_tick)), \
                mock.patch.object(sim.telephony, "place_call",
                                  noting("dial", sim.telephony.place_call)):
            self.assertEqual(sim.service.tick(incident.id, 0), "dialed")
        self.assertEqual(order, ["queue", "commit", "dial"])

    def test_a_tick_that_loses_the_commit_to_its_duplicate_does_not_dial(self):
        sim, incident = self.ready_to_redial()
        with mock.patch.object(sim.store, "advance_tick", return_value=False):
            self.assertEqual(sim.service.tick(incident.id, 0), "stale")
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)

    def test_a_tick_with_the_wrong_number_is_dropped_before_anything_is_queued(self):
        sim, incident = self.ready_to_redial()
        self.assertEqual(sim.service.tick(incident.id, 0), "dialed")
        waiting = len(sim.scheduler.queue)
        for seq in (0, 5):   # one already used, one from the future
            self.assertEqual(sim.service.tick(incident.id, seq), "stale")
        self.assertEqual(len(sim.scheduler.queue), waiting)
        self.assertEqual(len(sim.telephony.calls), 2)

    def test_the_fake_store_gives_each_step_to_one_caller_only(self):
        # Every duplicate-delivery test rests on the fake refusing the second
        # taker, the way the conditional writes in DynamoDB do.
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        now = sim.clock.now()
        self.assertEqual([sim.store.advance_tick(incident.id, 0, 1000) for _ in range(2)], [True, False])
        self.assertEqual([sim.store.claim_attempt(incident.id, 1, now) for _ in range(2)],
                         [True, False])
        sim.service.cancel(incident.id, "Robin")
        self.assertFalse(sim.store.advance_tick(incident.id, 1, 1000))
        self.assertFalse(sim.store.claim_attempt(incident.id, 2, now))


class CallInFlight(Edge):
    """While a call may still be ringing, a tick waits instead of dialing over
    it. The marker is trusted for a limited time, because the callback that
    clears it can get lost."""

    def test_a_tick_waits_out_the_grace_period_and_then_dials(self):
        sim = make(arm_offset_seconds=0)
        cfg = sim.cfg
        incident = sim.trigger()        # call 1; its status callback never arrives
        start = sim.clock.now()

        sim.clock.t = start + cfg.cycle_seconds
        self.assertEqual(sim.service.tick(incident.id, 0), "waiting")
        # It looks again soon, not a whole cycle later.
        self.assertIn((sim.clock.now() + cfg.busy_recheck_seconds, incident.id, 1), queued(sim))

        sim.clock.t = start + cfg.call_grace - 1
        self.assertEqual(sim.service.tick(incident.id, 1), "waiting")
        waited = sim.store.get(incident.id)
        self.assertEqual((len(sim.telephony.calls), waited.attempts), (1, 1))

        sim.clock.t = start + cfg.call_grace
        self.assertEqual(sim.service.tick(incident.id, 2), "dialed")
        self.assertEqual(len(sim.telephony.calls), 2)
        self.assertIn((sim.clock.now() + cfg.cycle_seconds, incident.id, 3), queued(sim))
        self.assertEqual(sim.store.get(incident.id).call_in_flight_since, sim.clock.now())

    def test_a_call_that_could_not_be_placed_is_not_waited_for(self):
        sim = make(arm_offset_seconds=0)
        sim.telephony.fail_next = True
        with self.assertLogs("er", level="ERROR") as logs:
            incident = sim.trigger()
        self.assertIn("call failed for " + incident.id, logs.output[0])
        failed = sim.store.get(incident.id)
        self.assertEqual((failed.attempts, failed.calls_failed, failed.call_in_flight_since),
                         (1, 1, None))
        sim.clock.t += sim.cfg.cycle_seconds
        self.assertEqual(sim.service.tick(incident.id, 0), "dialed")
        self.assertEqual(len(sim.telephony.calls), 1)

    def test_the_last_call_is_not_cut_short_by_expiry(self):
        # Expiring while the last call may still be on the line would throw
        # away the keypress that call is about to deliver.
        sim = make(arm_offset_seconds=0, primary_attempts=1)
        incident = sim.trigger()
        start = sim.clock.now()
        sim.clock.t = start + sim.cfg.cycle_seconds
        self.assertEqual(sim.service.tick(incident.id, 0), "waiting")
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)
        sim.clock.t = start + sim.cfg.call_grace
        self.assertEqual(sim.service.tick(incident.id, 1), "expired")
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)


class LostClaim(Edge):
    def test_a_tick_that_loses_the_claim_places_no_call(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "no-answer")
        sim.clock.t += sim.cfg.cycle_seconds
        with mock.patch.object(sim.store, "claim_attempt", return_value=False):
            sim.service.tick(incident.id, 0)
        self.assertEqual((len(sim.telephony.calls), sim.store.get(incident.id).attempts), (1, 1))

    def test_an_acknowledgment_that_lands_mid_tick_stops_the_dial(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "no-answer")
        sim.clock.t += sim.cfg.cycle_seconds
        commit = sim.store.advance_tick

        def commit_then_acknowledged(incident_id, seq, now):
            done = commit(incident_id, seq, now)
            sim.service.acknowledge(incident_id, via="pushover")
            return done

        with mock.patch.object(sim.store, "advance_tick", commit_then_acknowledged):
            sim.service.tick(incident.id, 0)
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)

    def test_a_trigger_that_loses_the_claim_still_sends_the_push(self):
        sim = make(arm_offset_seconds=0)
        with mock.patch.object(sim.store, "claim_attempt", return_value=False):
            status, _ = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(status, 201)
        self.assertEqual((len(sim.telephony.calls), len(sim.notifier.emergencies)), (0, 1))

    def test_an_acknowledgment_that_lands_as_the_incident_expires_wins(self):
        sim = make(arm_offset_seconds=0, primary_attempts=1)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "completed")
        sim.clock.t += sim.cfg.cycle_seconds
        read = sim.store.get
        acknowledged = []

        def read_then_acknowledged(incident_id):
            found = read(incident_id)
            if not acknowledged:
                acknowledged.append(True)
                sim.service.acknowledge(incident_id, via="call")
            return found

        with mock.patch.object(sim.store, "get", read_then_acknowledged):
            sim.service.tick(incident.id, 0)
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)
        # The sender hears that it was acknowledged, and is not then told
        # that nobody answered.
        self.assertEqual(sim.notifier.sender_messages,
                         [("Robin", "The owner acknowledged your alert.")])


class StatusCallback(Edge):
    """Twilio's end-of-call callback clears the in-flight marker, but only
    for the call that is in flight: callbacks arrive late and out of order."""

    def two_calls(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        self.assertEqual(no_answer_then_tick(sim, incident.id), "dialed")
        self.assertEqual(sim.store.get(incident.id).last_call_sid, "CA0002")
        return sim, incident

    def test_a_late_callback_for_an_earlier_call_leaves_the_marker(self):
        sim, incident = self.two_calls()
        sim.service.call_status(incident.id, "CA0001", "completed")
        self.assertEqual(sim.store.get(incident.id).call_in_flight_since, sim.clock.now())
        sim.service.call_status(incident.id, "CA0002", "no-answer")
        self.assertIsNone(sim.store.get(incident.id).call_in_flight_since)

    def test_a_callback_for_an_unknown_incident_is_ignored(self):
        sim = make()
        sim.service.call_status("x" * 16, "CA0001", "completed")
        self.assertEqual(sim.store.items, {})

    def test_the_route_needs_a_signature_and_clears_the_marker(self):
        sim, incident = self.two_calls()
        path = "/twilio/status/" + incident.id
        fields = {"CallSid": "CA0002", "CallStatus": "no-answer"}
        with self.assertLogs("er", level="WARNING") as logs:
            status, _ = call("POST", path, token=None, raw=urllib.parse.urlencode(fields))
        self.assertEqual(status, 403)
        self.assertIn("rejected a Twilio request to " + path, logs.output[0])
        self.assertIsNotNone(sim.store.get(incident.id).call_in_flight_since)

        self.assertEqual(answer(from_twilio(path, dict(fields, CallSid="CA0001"))),
                         (200, {"ok": True}))
        self.assertIsNotNone(sim.store.get(incident.id).call_in_flight_since)
        self.assertEqual(answer(from_twilio(path, fields)), (200, {"ok": True}))
        self.assertIsNone(sim.store.get(incident.id).call_in_flight_since)

    def test_a_signed_callback_for_an_unknown_incident_is_answered_so_twilio_stops(self):
        make()
        path = "/twilio/status/" + "x" * 16
        self.assertEqual(answer(from_twilio(path, {"CallSid": "CA1", "CallStatus": "completed"})),
                         (200, {"ok": True}))


# ------------------------------------------------------- who acknowledged

class Attribution(Edge):
    def test_a_keypress_on_a_call_to_the_backup_is_the_backups(self):
        sim = make(**WITH_BACKUP)
        sim.behaviour = answered_on(4)
        incident = sim.trigger()
        sim.run()
        closed = sim.store.get(incident.id)
        self.assertEqual((closed.status, closed.acked_via, closed.attempts),
                         (ACKNOWLEDGED, "call", 4))
        self.assertEqual(sim.telephony.calls[-1]["to"], BACKUP)
        self.assertEqual(sim.notifier.sender_messages,
                         [("Robin", "The backup contact acknowledged your alert.")])
        self.assertEqual(sim.service.describe(closed),
                         "The backup contact answered the call and acknowledged.")

    def test_a_keypress_on_the_last_call_to_the_owner_is_the_owners(self):
        sim = make(**WITH_BACKUP)
        sim.behaviour = answered_on(3)
        incident = sim.trigger()
        sim.run()
        closed = sim.store.get(incident.id)
        self.assertEqual((closed.status, closed.attempts), (ACKNOWLEDGED, 3))
        self.assertEqual(sim.notifier.sender_messages,
                         [("Robin", "The owner acknowledged your alert.")])
        self.assertEqual(sim.service.describe(closed),
                         "The owner answered the call and acknowledged.")

    def test_a_push_acknowledged_while_the_backup_rings_is_the_owners(self):
        # The push only ever goes to the owner, whoever the phone is ringing.
        sim = make(**WITH_BACKUP)
        sim.push_ack_after = 185      # the first call to the backup went out at 180
        incident = sim.trigger()
        sim.run()
        closed = sim.store.get(incident.id)
        self.assertEqual((closed.acked_via, closed.attempts), ("pushover", 4))
        self.assertEqual(sim.telephony.hung_up, ["CA0004"])
        self.assertEqual(sim.notifier.sender_messages,
                         [("Robin", "The owner acknowledged your alert.")])
        self.assertEqual(sim.service.describe(closed), "The owner acknowledged the alert.")

    def test_only_the_calls_to_the_backup_say_the_owner_has_not_responded(self):
        sim = make(**WITH_BACKUP)
        incident = sim.trigger()
        heard = []
        for _ in range(sim.cfg.total_attempts):
            heard.append("has not responded" in sim.service.voice_twiml(incident.id))
            no_answer_then_tick(sim, incident.id)
        self.assertEqual(heard, [False, False, False, True, True])
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)

    def test_what_the_backup_hears(self):
        sim = make(**WITH_BACKUP)
        incident = sim.trigger(note="Call me now.")
        for _ in range(3):
            no_answer_then_tick(sim, incident.id)
        self.assertEqual(sim.telephony.calls[-1]["to"], BACKUP)
        self.assertIn(
            "<Say>Emergency alert from Robin. The owner has not responded. "
            "Category: kids. Call me now. Press 1 to acknowledge.</Say>",
            sim.service.voice_twiml(incident.id),
        )

    def test_the_owners_name_is_used_wherever_the_owner_is_mentioned(self):
        sim = make(owner_name="alex", **WITH_BACKUP)
        sim.behaviour = answered_on(4)
        _, started = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(started["summary"], "Still alerting alex: no response yet, 1 call placed.")
        for _ in range(3):
            no_answer_then_tick(sim, started["id"])
        self.assertIn("Alex has not responded.", sim.service.voice_twiml(started["id"]))
        sim.service.acknowledge(started["id"], via="pushover")
        self.assertEqual(sim.notifier.sender_messages[-1][1], "Alex acknowledged your alert.")


class LateAndDuplicateAcknowledgments(Edge):
    def test_a_keypress_delivered_twice_counts_once(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        path = "/twilio/ack/" + incident.id
        keypress = from_twilio(path, {"Digits": "1", "CallSid": "CA0001"})
        self.assertEqual(answer(keypress)[0], 200)
        first = sim.store.get(incident.id)
        sim.clock.t += 5
        status, twiml = answer(keypress)
        self.assertEqual(status, 200)
        self.assertIn("<Hangup/>", twiml)
        again = sim.store.get(incident.id)
        self.assertEqual((again.status, again.acked_at, again.closed_at),
                         (ACKNOWLEDGED, first.acked_at, first.closed_at))
        self.assertEqual(len(sim.notifier.sender_messages), 1)
        self.assertEqual(sim.notifier.cancelled, ["rcpt-" + incident.id])
        self.assertFalse(sim.service.acknowledge(incident.id, via="call"))

    def test_a_keypress_after_a_cancel_does_not_reopen_or_relabel_the_incident(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        self.assertTrue(sim.service.cancel(incident.id, "Robin"))
        status, twiml = answer(from_twilio("/twilio/ack/" + incident.id, {"Digits": "1"}))
        self.assertEqual(status, 200)
        self.assertIn("<Hangup/>", twiml)       # the call ends either way
        closed = sim.store.get(incident.id)
        self.assertEqual((closed.status, closed.acked_via, closed.acked_at),
                         (CANCELLED, None, None))
        self.assertEqual(sim.notifier.sender_messages, [])

    def test_a_keypress_after_expiry_does_not_tell_the_sender_it_was_acknowledged(self):
        sim = make(arm_offset_seconds=0, primary_attempts=1)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)
        told = list(sim.notifier.sender_messages)
        answer(from_twilio("/twilio/ack/" + incident.id, {"Digits": "1"}))
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)
        self.assertEqual(sim.notifier.sender_messages, told)

    def test_a_push_acknowledgment_after_a_keypress_changes_nothing(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        receipt = sim.store.get(incident.id).pushover_receipt
        sim.service.acknowledge(incident.id, via="call")
        sim.notifier.acknowledged.add(receipt)
        status, body = call("POST", "/pushover/ack/" + incident.id, token=None,
                            raw=urllib.parse.urlencode({"acknowledged": "1", "receipt": receipt}))
        self.assertEqual((status, body), (200, {"acknowledged": False}))
        self.assertEqual(sim.store.get(incident.id).acked_via, "call")
        self.assertEqual(len(sim.notifier.sender_messages), 1)

    def test_a_push_callback_delivered_twice_counts_once(self):
        sim = make()
        incident = sim.trigger()
        receipt = sim.store.get(incident.id).pushover_receipt
        sim.notifier.acknowledged.add(receipt)
        raw = urllib.parse.urlencode({"acknowledged": "1", "receipt": receipt})
        path = "/pushover/ack/" + incident.id
        self.assertEqual(call("POST", path, token=None, raw=raw), (200, {"acknowledged": True}))
        self.assertEqual(call("POST", path, token=None, raw=raw), (200, {"acknowledged": False}))
        self.assertEqual(len(sim.notifier.sender_messages), 1)
        self.assertEqual(sim.notifier.cancelled, [receipt])

    def test_a_push_callback_for_an_unknown_incident_asks_pushover_nothing(self):
        sim = make()
        self.assertFalse(sim.service.acknowledge_from_push("x" * 16, "rcpt-" + "x" * 16))
        self.assertEqual(sim.notifier.lookups, 0)

    def test_an_empty_receipt_never_matches_an_incident_whose_push_has_not_gone_out(self):
        # With the call arm first there is no receipt yet. A forged callback
        # with a blank one must not be taken for a match.
        sim = make(first_arm="call")
        incident = sim.trigger()
        self.assertIsNone(sim.store.get(incident.id).pushover_receipt)
        sim.notifier.acknowledged.add("")
        for raw in ("acknowledged=1&receipt=", "acknowledged=1"):
            self.assertEqual(call("POST", "/pushover/ack/" + incident.id, token=None, raw=raw),
                             (200, {"acknowledged": False}), raw)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)
        self.assertEqual(sim.notifier.lookups, 0)

    def test_a_callback_that_is_not_an_acknowledgment_is_answered_and_ignored(self):
        sim = make()
        incident = sim.trigger()
        receipt = sim.store.get(incident.id).pushover_receipt
        sim.notifier.acknowledged.add(receipt)
        for raw in ("acknowledged=0&receipt=" + receipt, "receipt=" + receipt, ""):
            self.assertEqual(call("POST", "/pushover/ack/" + incident.id, token=None, raw=raw),
                             (200, {"ok": True}), raw)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)
        self.assertEqual(sim.notifier.lookups, 0)


# --------------------------------------------------------- the HTTP layer

class TestFlag(Edge):
    """Only true, "drill" and "full" make a test. Anything else a client
    might send in that field is a real alert: mislabelling an emergency as a
    drill is the worse mistake."""

    STRAY = (1, 0, 1.0, "true", "True", "yes", "TEST", "Drill", " drill", "FULL",
             ["drill"], {"full": True}, None, False, "")

    def test_the_three_values_that_mark_a_test(self):
        for value, expected in ((True, (True, False)), ("drill", (True, False)),
                                ("full", (True, True))):
            sim = make()
            status, body = call("POST", "/trigger", {"kind": "kids", "test": value})
            incident = sim.store.get(body["id"])
            self.assertEqual((status, incident.test, incident.rehearsal), (201,) + expected, value)
            self.assertEqual((body["test"], body["rehearsal"]), expected, value)

    def test_any_other_value_is_a_real_alert(self):
        for value in self.STRAY:
            sim = make()
            status, body = call("POST", "/trigger", {"kind": "kids", "test": value})
            incident = sim.store.get(body["id"])
            self.assertEqual((status, incident.test, incident.rehearsal, body["test"]),
                             (201, False, False, False), repr(value))
            self.assertIn("<Say>Emergency alert from Robin.", sim.service.voice_twiml(body["id"]))

    def test_a_break_glass_test_needs_exactly_true(self):
        # A labelled test leaves the real window alone, so anything looser
        # would let an agent's stray field switch the rate limit off.
        for value in ("true", 1, "yes", "drill"):
            sim = make()
            status, body = call("POST", "/break-glass", {"message": "m", "test": value},
                                token=ASSISTANT)
            self.assertEqual((status, body["retry_after_seconds"]), (200, 900), repr(value))
            self.assertEqual(sim.notifier.notices[0]["title"], "Assistant", repr(value))
            self.assertEqual(call("POST", "/break-glass", {"message": "m"}, token=ASSISTANT)[0],
                             429, repr(value))


class Base64Bodies(Edge):
    """A Function URL may hand over the body base64-encoded, and does so for
    the form posts that Twilio and Pushover send."""

    def test_a_json_trigger(self):
        sim = make()
        body = json.dumps({"kind": "kids", "note": "Café fermé, appelle"}, ensure_ascii=False)
        status, made = call("POST", "/trigger", raw=b64(body), encoded=True)
        self.assertEqual(status, 201)
        incident = sim.store.get(made["id"])
        self.assertEqual((incident.kind, incident.note), ("kids", "Café fermé, appelle"))

    def test_a_signed_keypress_from_twilio(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        keypress = from_twilio("/twilio/ack/" + incident.id,
                               {"Digits": "1", "CallSid": "CA0001"}, encoded=True)
        self.assertEqual(answer(keypress)[0], 200)
        self.assertEqual(sim.store.get(incident.id).acked_via, "call")

    def test_an_acknowledgment_from_pushover(self):
        sim = make()
        incident = sim.trigger()
        receipt = sim.store.get(incident.id).pushover_receipt
        sim.notifier.acknowledged.add(receipt)
        raw = b64(urllib.parse.urlencode({"acknowledged": "1", "receipt": receipt}))
        status, body = call("POST", "/pushover/ack/" + incident.id, token=None,
                            raw=raw, encoded=True)
        self.assertEqual((status, body), (200, {"acknowledged": True}))
        self.assertEqual(sim.store.get(incident.id).acked_via, "pushover")


class BearerTokens(Edge):
    TOKEN_ROUTES = (
        ("POST", "/trigger"), ("POST", "/pause"), ("POST", "/resume"), ("POST", "/self-test"),
        ("POST", "/break-glass"), ("GET", "/break-glass/status"),
    )

    def test_an_entry_with_a_blank_token_lets_nobody_in(self):
        # A blank token in the secret must not match a request with no token.
        sim = make(trigger_tokens={"Robin": ""}, break_glass_tokens={"Assistant": ""})
        for method, path in self.TOKEN_ROUTES:
            for headers in ({}, {"Authorization": ""}, {"Authorization": "Bearer"},
                            {"Authorization": "Bearer "}, {"Authorization": "Bearer   "}):
                response = request(method, path, body={"kind": "kids", "message": "m"},
                                   headers=headers)
                self.assertEqual(response["statusCode"], 401, (method, path, headers))
        self.assertEqual((sim.store.items, sim.notifier.notices), ({}, []))
        self.assertEqual(sim.service.paused_seconds(), 0)

    def test_the_header_name_and_the_word_bearer_may_be_in_any_case(self):
        for headers in ({"authorization": "bearer " + ROBIN},
                        {"AUTHORIZATION": "BEARER " + ROBIN},
                        {"Authorization": "Bearer  " + ROBIN + " "}):
            make()
            response = request("POST", "/trigger", body={"kind": "kids"}, headers=headers)
            self.assertEqual(response["statusCode"], 201, headers)

    def test_a_token_sent_any_other_way_is_refused(self):
        sim = make()
        basic = base64.b64encode(("Robin:" + ROBIN).encode()).decode()
        for headers in ({"Authorization": ROBIN}, {"Authorization": "Basic " + basic},
                        {"Authorization": "Token " + ROBIN}, {"X-Api-Key": ROBIN}):
            response = request("POST", "/trigger", body={"kind": "kids"}, headers=headers)
            self.assertEqual(response["statusCode"], 401, headers)
        self.assertEqual(sim.store.items, {})

    def test_a_token_in_the_body_or_the_query_string_is_not_a_token(self):
        sim = make()
        made = event("POST", "/trigger", body={"kind": "kids", "token": ROBIN})
        made["rawQueryString"] = "token=" + ROBIN
        self.assertEqual(handler.lambda_handler(made)["statusCode"], 401)
        self.assertEqual(sim.store.items, {})


class Cancel(Edge):
    def test_only_the_sender_who_started_it_can_cancel_and_only_once(self):
        sim = make(arm_offset_seconds=0, trigger_tokens={"Robin": ROBIN, "Jo": JO})
        _, started = call("POST", "/trigger", {"kind": "kids"})
        path = "/incidents/%s/cancel" % started["id"]
        self.assertEqual(call("POST", path, token=None)[0], 401)
        self.assertEqual(call("POST", path, token=ASSISTANT)[0], 401)
        self.assertEqual(call("POST", path, token=JO), (409, {"cancelled": False}))
        self.assertEqual(sim.store.get(started["id"]).status, ACTIVE)
        self.assertEqual((sim.telephony.hung_up, sim.notifier.cancelled), ([], []))

        self.assertEqual(call("POST", path), (200, {"cancelled": True}))
        self.assertEqual(call("POST", path), (409, {"cancelled": False}))
        self.assertEqual(sim.store.get(started["id"]).status, CANCELLED)
        self.assertEqual(sim.telephony.hung_up, ["CA0001"])
        self.assertEqual(sim.notifier.cancelled, ["rcpt-" + started["id"]])

    def test_another_sender_can_still_follow_and_join_the_incident(self):
        # One incident at a time: a second sender's tap joins the first.
        sim = make(trigger_tokens={"Robin": ROBIN, "Jo": JO})
        _, started = call("POST", "/trigger", {"kind": "kids"})
        status, joined = call("POST", "/trigger", {"kind": "house"}, token=JO)
        self.assertEqual((status, joined["id"], joined["kind"]), (200, started["id"], "kids"))
        self.assertEqual(call("GET", "/incidents/" + started["id"], token=JO)[0], 200)
        self.assertEqual(len(sim.notifier.emergencies), 1)

    def test_an_unknown_incident(self):
        make()
        unknown = "x" * 16
        self.assertEqual(call("POST", "/incidents/%s/cancel" % unknown),
                         (409, {"cancelled": False}))
        self.assertEqual(call("GET", "/incidents/" + unknown)[0], 404)
        self.assertEqual(call("GET", "/incidents/short")[0], 404)


class WrongMethod(Edge):
    def test_a_route_answers_only_to_its_own_method(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        for method, path, token in (
            ("GET", "/trigger", ROBIN),
            ("GET", "/pause", ROBIN),
            ("GET", "/break-glass", ASSISTANT),
            ("POST", "/break-glass/status", ASSISTANT),
            ("POST", "/incidents/" + incident.id, ROBIN),
            ("GET", "/incidents/%s/cancel" % incident.id, ROBIN),
            ("DELETE", "/incidents/" + incident.id, ROBIN),
        ):
            self.assertEqual(call(method, path, {"kind": "kids", "message": "m"}, token=token)[0],
                             404, (method, path))
        signed = from_twilio("/twilio/ack/" + incident.id, {"Digits": "1"})
        signed["requestContext"]["http"]["method"] = "GET"
        self.assertEqual(answer(signed)[0], 404)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)
        self.assertEqual(len(sim.store.items), 1)
        self.assertEqual(sim.notifier.notices, [])
        self.assertEqual(sim.service.paused_seconds(), 0)


class TwilioSignatures(Edge):
    def test_no_auth_token_configured_means_no_twilio_request_is_accepted(self):
        # A push-only deployment has no Twilio secret. A request signed with
        # an empty key must not pass for a signed one.
        sim = make(call_enabled=False, primary_number="", twilio_auth_token="")
        incident = sim.trigger()
        with self.assertLogs("er", level="WARNING") as logs:
            for route in ("ack", "voice", "status"):
                forged = from_twilio("/twilio/%s/%s" % (route, incident.id), {"Digits": "1"}, key="")
                self.assertEqual(answer(forged)[0], 403, route)
        self.assertEqual(len(logs.output), 3)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)

    def test_a_signed_request_is_good_for_its_own_route_and_incident_only(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        voice, ack = "/twilio/voice/" + incident.id, "/twilio/ack/" + incident.id
        fields = {"CallSid": "CA0001"}
        self.assertEqual(answer(from_twilio(voice, fields))[0], 200)

        with self.assertLogs("er", level="WARNING") as logs:
            # A real voice webhook replayed against the keypress route.
            replayed = from_twilio(voice, fields)
            replayed["rawPath"] = ack
            self.assertEqual(answer(replayed)[0], 403)

            # A real webhook with a keypress added to it afterwards.
            tampered = from_twilio(ack, fields)
            tampered["body"] = urllib.parse.urlencode(dict(fields, Digits="1"))
            self.assertEqual(answer(tampered)[0], 403)

            # Another incident's keypress replayed against this one.
            elsewhere = from_twilio("/twilio/ack/" + "y" * 16, {"Digits": "1"})
            elsewhere["rawPath"] = ack
            self.assertEqual(answer(elsewhere)[0], 403)
        self.assertEqual(len(logs.output), 3)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)


class SmallBodies(Edge):
    def test_a_trigger_with_no_body_or_an_odd_one_still_starts_an_alert(self):
        # Under stress the alert matters more than the form it came in.
        for raw in ("", "[1]", '"kids"', "7", "null"):
            sim = make()
            status, body = call("POST", "/trigger", raw=raw)
            self.assertEqual((status, body["kind"]), (201, "emergency"), raw)
            self.assertEqual(len(sim.notifier.emergencies), 1, raw)

    def test_a_form_post_to_the_trigger_is_refused_and_starts_nothing(self):
        sim = make()
        self.assertEqual(call("POST", "/trigger", raw="kind=kids"),
                         (400, {"error": "body must be JSON"}))
        self.assertEqual(sim.store.items, {})

    def test_long_text_is_cut_to_what_a_call_can_say(self):
        sim = make()
        _, body = call("POST", "/trigger", {"kind": "k" * 500, "note": "n" * 5000})
        incident = sim.store.get(body["id"])
        self.assertEqual((len(incident.kind), len(incident.note)), (40, 200))

    def test_how_long_a_pause_lasts(self):
        sim = make()
        with self.assertLogs("er", level="WARNING"):
            for minutes, seconds in ((None, 3600), (0, 3600), ("", 3600), (15, 900), ("15", 900),
                                     (1, 60), (-5, 60), (1.9, 60)):
                status, body = call("POST", "/pause", {"minutes": minutes})
                self.assertEqual((status, body["paused_seconds"]), (200, seconds), repr(minutes))
                self.assertEqual(body["paused_until"], sim.clock.now() + seconds)
            self.assertEqual(call("POST", "/pause", raw="")[1]["paused_seconds"], 3600)

    def test_a_pause_that_cannot_be_read_as_minutes_changes_nothing(self):
        sim = make()
        for minutes in ([1], {"m": 1}, "soon"):
            self.assertEqual(call("POST", "/pause", {"minutes": minutes}),
                             (400, {"error": "minutes must be a number"}), repr(minutes))
        self.assertEqual(sim.service.paused_seconds(), 0)

    def test_without_a_clock_the_service_uses_the_time_of_day(self):
        sim = make()
        clockless = Service(sim.cfg, sim.store, sim.telephony, sim.notifier, sim.scheduler)
        before = int(time.time())
        incident, _ = clockless.trigger("Robin", "kids", BASE)
        self.assertIsInstance(incident.created_at, int)
        self.assertTrue(before <= incident.created_at <= int(time.time()) + 1)


class WhatTheCallerIsShown(Edge):
    """The replies go to a sender's phone or an agent. They carry the state
    of the incident and nothing about where the owner is or how the alert
    travels."""

    FIELDS = {"id", "status", "kind", "attempts", "calls_failed", "created_at", "acked_via",
              "acked_at", "test", "rehearsal", "summary"}
    PRIVATE = ("Call me now", "47.61", "122.33", "rcpt-", "CA0001", DOMAIN, PRIMARY, "Robin")

    def body(self, response):
        for private in self.PRIVATE:
            self.assertNotIn(private, response["body"])
        return json.loads(response["body"])

    def test_the_replies_have_exactly_these_fields(self):
        make(arm_offset_seconds=0)
        details = {"kind": "kids", "note": "Call me now.", "lat": 47.61, "lon": -122.33}
        started = self.body(request("POST", "/trigger", body=details, token=ROBIN))
        self.assertEqual(set(started), self.FIELDS | {"created"})
        joined = self.body(request("POST", "/trigger", body=details, token=ROBIN))
        self.assertEqual(set(joined), self.FIELDS | {"created"})
        self.assertEqual((started["created"], joined["created"]), (True, False))

        path = "/incidents/" + started["id"]
        self.assertEqual(set(self.body(request("GET", path, token=ROBIN))), self.FIELDS)
        call("POST", path + "/cancel")
        self.assertEqual(set(self.body(request("GET", path, token=ROBIN))), self.FIELDS | {"final"})

    def test_a_self_test_reply_has_the_same_fields(self):
        make(arm_offset_seconds=0)
        started = self.body(request("POST", "/self-test", body={}, token=ASSISTANT))
        self.assertEqual(set(started), self.FIELDS | {"started"})
        self.assertTrue(started["started"])
        followed = self.body(request("GET", "/incidents/" + started["id"], token=ASSISTANT))
        self.assertEqual(set(followed), self.FIELDS)

    def test_an_agent_can_follow_a_self_test_but_not_a_senders_drill(self):
        sim = make()
        _, drill = call("POST", "/trigger", {"kind": "kids", "test": True})
        self.assertEqual(call("GET", "/incidents/" + drill["id"], token=ASSISTANT),
                         (404, {"error": "not found"}))
        sim.service.cancel(drill["id"], "Robin")
        _, by_sender = call("POST", "/self-test", {})
        self.assertEqual(sim.store.get(by_sender["id"]).sender, SELF_TEST_SENDER)
        self.assertEqual(call("GET", "/incidents/" + by_sender["id"], token=ASSISTANT)[0], 200)

    def test_an_internal_error_is_logged_in_full_and_answered_in_brief(self):
        sim = make()
        failure = RuntimeError("table emergency-ring-incidents is throttled")
        with mock.patch.object(sim.store, "create_if_none_active", side_effect=failure), \
                self.assertLogs("er", level="ERROR") as logs:
            status, body = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual((status, body), (500, {"error": "internal error"}))
        self.assertIn("unhandled error", logs.output[0])
        self.assertIn("is throttled", logs.output[0])


# --------------------------------------------------- pause and self-test

class PauseAndSelfTest(Edge):
    def test_the_pause_is_in_force_before_the_active_incident_is_stopped(self):
        # The other way round, a trigger could start in the gap and ring on.
        sim = make()
        sim.trigger()
        seen = []
        stop = sim.store.transition

        def stop_and_note(*args, **fields):
            seen.append(sim.service.paused_seconds())
            return stop(*args, **fields)

        with mock.patch.object(sim.store, "transition", stop_and_note), \
                self.assertLogs("er", level="WARNING") as logs:
            sim.service.pause("Robin", 600)
        self.assertEqual(seen, [600])
        self.assertIn("paused by Robin for 600s", logs.output[0])

    def test_a_push_waiting_in_the_queue_is_not_sent_once_alerts_are_paused(self):
        sim = make(first_arm="call")
        incident = sim.trigger()
        # A pause that was being set as the trigger came in.
        sim.store.set_pause(sim.clock.now() + 600, "Robin")
        self.assertEqual(sim.service.deliver_push(incident.id), "paused")
        self.assertEqual(sim.notifier.emergencies, [])
        stopped = sim.store.get(incident.id)
        self.assertEqual((stopped.status, stopped.cancel_reason), (CANCELLED, "paused"))
        self.assertEqual(sim.telephony.hung_up, ["CA0001"])

    def test_a_push_waiting_in_the_queue_is_not_sent_after_the_incident_closed(self):
        sim = make(first_arm="call")
        incident = sim.trigger()
        sim.service.acknowledge(incident.id, via="call")
        self.assertEqual(sim.service.deliver_push(incident.id), "stopped")
        self.assertEqual(sim.notifier.emergencies, [])

    def test_a_self_test_that_fails_to_start_does_not_use_up_the_agents_turn(self):
        sim = make()
        with mock.patch.object(sim.scheduler, "schedule_tick", boom), \
                self.assertLogs("er", level="ERROR") as logs:
            status, _ = call("POST", "/self-test", {}, token=ASSISTANT)
        self.assertEqual(status, 500)
        self.assertIn("unhandled error", logs.output[0])
        self.assertEqual(sim.notifier.emergencies, [])

        status, body = call("POST", "/self-test", {}, token=ASSISTANT)
        self.assertEqual((status, body["started"]), (201, True))
        # That one did start, so the turn is now used.
        sim.service.acknowledge(body["id"], via="call")
        status, body = call("POST", "/self-test", {}, token=ASSISTANT)
        self.assertEqual((status, body["reason"]), (429, "interval"))

    def test_a_senders_self_test_gives_way_to_an_active_incident(self):
        sim = make()
        _, real = call("POST", "/trigger", {"kind": "kids"})
        response = request("POST", "/self-test", body={}, token=ROBIN)
        self.assertEqual(response["statusCode"], 409)
        self.assertEqual(response["headers"]["retry-after"], str(sim.cfg.cycle_seconds))
        self.assertEqual(json.loads(response["body"]),
                         {"reason": "busy", "retry_after_seconds": sim.cfg.cycle_seconds,
                          "started": False})
        self.assertFalse(sim.store.get(real["id"]).test)
        self.assertEqual(len(sim.notifier.emergencies), 1)

    def test_the_stacks_own_timer_is_not_held_to_one_a_day(self):
        sim = make()
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE}):
            first = handler.lambda_handler({"action": "selftest"})
            sim.service.acknowledge(first["incident"], via="call")
            sim.clock.t += 3600
            second = handler.lambda_handler({"action": "selftest"})
            self.assertNotEqual(second["incident"], first["incident"])
            # While that one runs, the timer firing again starts nothing new.
            busy = handler.lambda_handler({"action": "selftest"})
        self.assertEqual(busy, {"reason": "busy", "retry_after_seconds": sim.cfg.cycle_seconds})
        self.assertEqual(len(sim.notifier.emergencies), 2)


# ------------------------------------------------------- failures are logged

class LoggedFailures(Edge):
    """A failure the code survives leaves one trace: the log line. These
    check that it is written and names the incident."""

    def test_a_failed_receipt_lookup_is_logged_and_the_tick_dials_anyway(self):
        sim = make()
        incident = sim.trigger()
        sim.notifier.lookup_fails = True
        sim.clock.t += sim.cfg.call_delay
        with self.assertLogs("er", level="ERROR") as logs:
            self.assertEqual(sim.service.tick(incident.id, 0), "dialed")
        self.assertIn("push receipt lookup failed for " + incident.id, logs.output[0])
        self.assertEqual(len(sim.telephony.calls), 1)

    def test_a_failed_first_push_is_logged_and_queued_again(self):
        sim = make()
        with mock.patch.object(sim.notifier, "send_emergency", boom), \
                self.assertLogs("er", level="ERROR") as logs:
            status, body = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(status, 502)
        self.assertIn("push alert failed for %s; queueing a retry" % body["incident_id"],
                      logs.output[0])
        self.assertEqual(len(sim.scheduler.pushes), 1)

    def test_each_failed_goodbye_is_logged_and_the_incident_still_closes(self):
        sim = make(arm_offset_seconds=0, sender_pushover_keys={"Robin": "robin-user-key"})
        incident = sim.trigger()
        with mock.patch.object(sim.notifier, "cancel_emergency", boom), \
                mock.patch.object(sim.telephony, "hangup", boom), \
                mock.patch.object(sim.notifier, "notify_sender", boom), \
                self.assertLogs("er", level="ERROR") as logs:
            stopped = sim.service.pause("Assistant", 600)["stopped_incident"]
        self.assertEqual(stopped, incident.id)
        self.assertEqual(sim.store.get(incident.id).status, CANCELLED)
        logged = sorted(line.split("\n")[0] for line in logs.output)
        self.assertEqual(logged, [
            "ERROR:er:could not hangup for " + incident.id,
            "ERROR:er:could not notify sender for " + incident.id,
            "ERROR:er:could not retract push for " + incident.id,
        ])

    def test_an_alert_that_went_out_but_could_not_be_recorded_does_not_stop_the_other_arm(self):
        sim = make(arm_offset_seconds=0)
        write = sim.store.update

        def write_fails_for_the_receipt(incident_id, **fields):
            if "pushover_receipt" in fields:
                raise RuntimeError("write throttled")
            return write(incident_id, **fields)

        with mock.patch.object(sim.store, "update", write_fails_for_the_receipt), \
                self.assertLogs("er", level="ERROR") as logs:
            status, body = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(status, 201)
        # Tried twice, then logged and left: the call is not held up by it.
        self.assertIn("trying once more", logs.output[0])
        self.assertIn("could not record an alert for " + body["id"], logs.output[1])
        self.assertEqual((len(sim.telephony.calls), len(sim.notifier.emergencies)), (1, 1))

    # Without the receipt the service cannot recognise an acknowledgment in
    # the app, has nothing to ask Pushover about and cannot take the alert
    # back. So the write that stores it is tried twice.
    def test_an_acknowledgment_counts_even_if_storing_the_receipt_failed_once(self):
        sim = make(call_enabled=False, primary_number="", twilio_auth_token="")
        sim.push_ack_after = 20      # the owner acknowledges in the app
        write = sim.store.update
        failed = []

        def write_fails_once_for_the_receipt(incident_id, **fields):
            if "pushover_receipt" in fields and not failed:
                failed.append(True)
                raise RuntimeError("write throttled")
            return write(incident_id, **fields)

        # The log is silenced, not asserted: a fix may well change what is logged.
        with mock.patch.object(sim.store, "update", write_fails_once_for_the_receipt), \
                mock.patch("er.core.log"):
            incident = sim.trigger()
            sim.run()
        self.assertTrue(sim.notifier.emergencies)       # the alert did go out
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)

    def test_a_start_that_can_be_neither_followed_up_nor_undone_reports_the_first_failure(self):
        sim = make()

        def queue_down(*args):
            raise RuntimeError("queue unavailable")

        with mock.patch.object(sim.scheduler, "schedule_tick", queue_down), \
                mock.patch.object(sim.store, "transition", boom), \
                self.assertLogs("er", level="ERROR") as logs:
            with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
                sim.service.trigger("Robin", "kids", BASE)
        self.assertIn("could not close incident", logs.output[0])
        self.assertEqual((sim.notifier.emergencies, sim.telephony.calls), ([], []))


# -------------------------------------------------------------- cold start

class ColdStart(Edge):
    """How the service comes to exist: built once, on Lambda before the first
    request, and built again by the next request if that failed."""

    ENV = {
        "SSM_PREFIX": "/emergency-ring",
        "TABLE_NAME": "incidents",
        "TICK_QUEUE_URL": "https://sqs.example.invalid/ticks",
        "PRIMARY_NUMBER": PRIMARY,
        "TWILIO_ACCOUNT_SID": "ACexample",
        "TWILIO_FROM_NUMBER": "+15550000009",
    }
    SECRETS = {
        "twilio-auth-token": TWILIO_TOKEN,
        "trigger-tokens": json.dumps({"Robin": ROBIN}),
        "pushover-app-token": "app-token",
        "pushover-user-key": "user-key",
    }

    def setUp(self):
        super().setUp()
        os.environ.update(self.ENV)
        os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
        self.secrets_exist = True

        def load(prefix, required):
            if not self.secrets_exist:
                raise RuntimeError("missing SSM parameters")
            return self.SECRETS

        # SSM, DynamoDB and SQS are replaced, so building reaches nothing.
        self.load = mock.Mock(side_effect=load)
        for target, stand_in in (
            ("er.ssm_secrets.load_secrets", self.load),
            ("er.aws_adapters.DynamoStore", mock.MagicMock()),
            ("er.aws_adapters.SqsScheduler", mock.MagicMock()),
        ):
            patcher = mock.patch(target, stand_in)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_handler_from_the_top(self, on_lambda):
        """Execute handler.py the way a new Lambda environment does, as a
        second copy of the module, so the one the other tests use is left
        as it was."""
        spec = importlib.util.spec_from_file_location("er.handler_second_copy", handler.__file__)
        module = importlib.util.module_from_spec(spec)
        runtime = {"AWS_LAMBDA_FUNCTION_NAME": "emergency-ring"} if on_lambda else {}
        with mock.patch.dict(os.environ, runtime):
            spec.loader.exec_module(module)
        return module

    def test_on_lambda_the_service_is_built_before_the_first_request(self):
        module = self.run_handler_from_the_top(on_lambda=True)
        self.load.assert_called_once_with("/emergency-ring", required=mock.ANY)
        self.assertEqual(module.lambda_handler(event("GET", "/"))["statusCode"], 404)
        self.assertEqual(self.load.call_count, 1)

    def test_anywhere_else_loading_the_handler_builds_nothing(self):
        self.run_handler_from_the_top(on_lambda=False)
        self.load.assert_not_called()

    def test_a_failed_start_is_logged_and_the_next_request_tries_again(self):
        self.secrets_exist = False
        with self.assertLogs("er", level="ERROR") as logs:
            module = self.run_handler_from_the_top(on_lambda=True)
        self.assertIn("could not start during init", logs.output[0])

        trigger = event("POST", "/trigger", body={"kind": "kids"}, token=ROBIN)
        with self.assertLogs("er", level="ERROR") as logs:
            self.assertEqual(module.lambda_handler(trigger)["statusCode"], 500)
        self.assertIn("unhandled error", logs.output[0])
        # A queue message is raised, so SQS keeps it for the next try.
        with self.assertRaises(RuntimeError):
            module.lambda_handler(records({"incident_id": "x" * 16, "seq": 0}))

        self.secrets_exist = True
        self.assertEqual(module.lambda_handler(event("GET", "/"))["statusCode"], 404)
        self.assertEqual(self.load.call_count, 4)
        self.assertEqual(module.lambda_handler(event("GET", "/"))["statusCode"], 404)
        self.assertEqual(self.load.call_count, 4)

    def test_the_first_request_builds_the_service_and_later_ones_reuse_it(self):
        sim = make()
        # Later tests in this process must find a service built on fakes.
        self.addCleanup(handler.install, sim.service)
        handler.install(None)
        with mock.patch.object(handler, "build_service", return_value=sim.service) as build:
            self.assertEqual(call("GET", "/")[0], 404)
            self.assertEqual(call("POST", "/trigger", {"kind": "kids"})[0], 201)
        build.assert_called_once_with()

    def test_a_service_that_cannot_be_built_fails_every_kind_of_event_loudly(self):
        sim = make()
        self.addCleanup(handler.install, sim.service)
        handler.install(None)
        with mock.patch.object(handler, "build_service", side_effect=RuntimeError("no secrets")):
            with self.assertLogs("er", level="ERROR"):
                self.assertEqual(call("POST", "/trigger", {"kind": "kids"}),
                                 (500, {"error": "internal error"}))
            with self.assertRaises(RuntimeError):
                handler.lambda_handler(records({"incident_id": "x" * 16, "seq": 0}))
            with self.assertRaises(RuntimeError):
                handler.lambda_handler({"action": "selftest"})


if __name__ == "__main__":
    unittest.main()
