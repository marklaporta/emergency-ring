"""When something goes wrong: a vendor is down, the store hiccups, a message
arrives twice, or a stranger posts junk. None of it may leave an incident
that looks alive and alerts nobody."""
import base64
import json
import os
import unittest
import urllib.parse
import xml.dom.minidom
from unittest import mock

from support import ASSISTANT, BASE, PRIMARY, ROBIN, TWILIO_TOKEN, call, make

from er import handler
from er.config import Config
from er.core import MAX_PAUSE_SECONDS, PUSH_RETRY_SECONDS, side_by_side
from er.models import ACKNOWLEDGED, ACTIVE, CANCELLED, EXPIRED
from er.twilio_adapter import compute_signature


def boom(*args, **kwargs):
    raise RuntimeError("vendor unavailable")


class Quiet(unittest.TestCase):
    """These tests make the code log errors on purpose; keep the output clean."""

    def setUp(self):
        os.environ.pop("PUBLIC_BASE_URL", None)
        for module in ("er.core.log", "er.handler.log"):
            patcher = mock.patch(module)
            patcher.start()
            self.addCleanup(patcher.stop)


class RealAlertDuringATest(Quiet):
    def test_a_real_trigger_replaces_a_running_drill(self):
        sim = make(arm_offset_seconds=0)
        _, drill = call("POST", "/trigger", {"kind": "kids", "test": True})
        status, real = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(status, 201)
        self.assertNotEqual(real["id"], drill["id"])
        self.assertFalse(real["test"])
        self.assertEqual(sim.store.get(drill["id"]).status, CANCELLED)
        # The drill's own alerts are taken back, and the real ones go out.
        self.assertEqual(sim.notifier.cancelled, ["rcpt-" + drill["id"]])
        self.assertEqual(sim.telephony.hung_up, ["CA0001"])
        self.assertEqual(len(sim.notifier.emergencies), 2)
        self.assertEqual(len(sim.telephony.calls), 2)
        _, view = call("GET", "/incidents/" + drill["id"])
        self.assertEqual(view["final"], "Stopped: a real alert replaced this test.")

    def test_a_real_trigger_replaces_an_agents_self_test(self):
        sim = make()
        _, test = call("POST", "/self-test", {}, token=ASSISTANT)
        status, real = call("POST", "/trigger", {"kind": "me"})
        self.assertEqual((status, real["test"]), (201, False))
        self.assertEqual(sim.store.get(test["id"]).status, CANCELLED)

    def test_a_drill_joins_a_real_alert_and_changes_nothing(self):
        sim = make()
        _, real = call("POST", "/trigger", {"kind": "kids"})
        status, drill = call("POST", "/trigger", {"kind": "kids", "test": True})
        self.assertEqual((status, drill["id"], drill["test"]), (200, real["id"], False))
        self.assertEqual(sim.store.get(real["id"]).status, ACTIVE)
        self.assertEqual(len(sim.notifier.emergencies), 1)


class NothingGoesOut(Quiet):
    def test_a_trigger_that_cannot_be_followed_up_does_not_stay_active(self):
        sim = make()
        with mock.patch.object(sim.scheduler, "schedule_tick", boom):
            status, _ = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(status, 500)
        self.assertIsNone(sim.store.active_id())
        self.assertEqual(len(sim.notifier.emergencies), 0)
        # The sender's next tap starts a working incident, not a dead one.
        status, body = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual((status, body["created"]), (201, True))
        self.assertEqual(len(sim.notifier.emergencies), 1)

    def test_push_only_and_the_push_fails(self):
        sim = make(call_enabled=False, primary_number="")
        with mock.patch.object(sim.notifier, "send_emergency", boom):
            status, body = call("POST", "/trigger", {"kind": "kids"})
        # No "id": the sender's Shortcut takes this as failure and falls back.
        self.assertEqual(status, 502)
        self.assertNotIn("id", body)
        self.assertIn("Nothing has reached", body["summary"])
        incident = sim.store.get(body["incident_id"])
        self.assertEqual(incident.status, ACTIVE)
        # The queue retries it a few seconds later.
        self.assertEqual([due - sim.clock.now() for due, _, _ in sim.scheduler.pushes],
                         [PUSH_RETRY_SECONDS])
        sim.clock.t += PUSH_RETRY_SECONDS
        self.assertEqual(sim.service.deliver_push(incident.id), "sent")
        self.assertEqual(len(sim.notifier.emergencies), 1)
        _, view = call("GET", "/incidents/" + incident.id)
        self.assertEqual(view["summary"], "Still alerting the owner: no response yet.")

    def test_the_first_push_fails_and_the_call_still_goes_out(self):
        sim = make()
        with mock.patch.object(sim.notifier, "send_emergency", boom):
            status, body = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(status, 502)
        sim.run()
        self.assertEqual(len(sim.notifier.emergencies), 1)   # the retry
        self.assertEqual(len(sim.telephony.calls), 5)
        self.assertEqual(sim.store.get(body["incident_id"]).status, EXPIRED)

    def test_a_queued_push_that_keeps_failing_raises_so_the_queue_redelivers(self):
        sim = make(call_enabled=False, primary_number="")
        with mock.patch.object(sim.notifier, "send_emergency", boom):
            _, body = call("POST", "/trigger", {"kind": "kids"})
            with self.assertRaises(RuntimeError):
                handler.lambda_handler({"Records": [{"body": json.dumps(
                    {"incident_id": body["incident_id"], "action": "push"})}]})

    def test_calls_that_cannot_be_placed_are_not_reported_as_placed(self):
        sim = make(push_enabled=False, primary_attempts=3)
        with mock.patch.object(sim.telephony, "place_call", boom):
            status, body = call("POST", "/trigger", {"kind": "kids"})
            self.assertEqual(status, 502)
            self.assertNotIn("placed", body["summary"])
            sim.run()
        incident = sim.store.get(body["incident_id"])
        self.assertEqual((incident.status, incident.attempts, incident.calls_failed), (EXPIRED, 3, 3))
        self.assertEqual(sim.notifier.sender_messages[-1][1],
                         "No call could be placed, and nobody acknowledged your alert.")
        _, view = call("GET", "/incidents/" + incident.id)
        self.assertEqual((view["attempts"], view["calls_failed"]), (3, 3))

    def test_one_failed_call_is_left_out_of_the_count(self):
        sim = make(push_enabled=False, primary_attempts=3)
        sim.telephony.fail_next = True
        _, body = call("POST", "/trigger", {"kind": "kids"})
        sim.run()
        self.assertEqual(sim.notifier.sender_messages[-1][1],
                         "Nobody acknowledged your alert after 2 calls (1 more could not be placed).")

    def test_a_store_error_on_the_call_arm_does_not_stop_the_push(self):
        sim = make(arm_offset_seconds=0)
        with mock.patch.object(sim.store, "claim_attempt", boom):
            status, body = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(status, 201)
        self.assertEqual(len(sim.notifier.emergencies), 1)
        self.assertEqual(len(sim.telephony.calls), 0)
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 5)   # the ticks carry on


class ClosingUnderFailure(Quiet):
    def expiring(self):
        sim = make(push_enabled=False, primary_attempts=1)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "no-answer")
        sim.clock.t += sim.cfg.cycle_seconds
        return sim, incident

    def test_a_store_error_at_expiry_is_retried_by_the_same_tick(self):
        sim, incident = self.expiring()
        real = sim.store.transition
        with mock.patch.object(sim.store, "transition", boom):
            with self.assertRaises(RuntimeError):
                sim.service.tick(incident.id, 0)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)
        self.assertEqual(sim.service.tick(incident.id, 0), "expired")   # the redelivery
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)
        self.assertTrue(sim.notifier.sender_messages[-1][1].startswith("Nobody acknowledged"))
        self.assertTrue(sim.service.trigger("Robin", "kids", BASE)[1])
        self.assertIs(real.__self__, sim.store)

    def test_an_expiry_tick_delivered_twice_tells_the_sender_once(self):
        sim, incident = self.expiring()
        self.assertEqual(sim.service.tick(incident.id, 0), "expired")
        self.assertEqual(sim.service.tick(incident.id, 0), "stopped")
        self.assertEqual(len(sim.notifier.sender_messages), 1)

    def test_one_failing_goodbye_does_not_stop_the_others(self):
        sim = make(arm_offset_seconds=0, sender_pushover_keys={"Robin": "u"})
        incident = sim.trigger()
        with mock.patch.object(sim.notifier, "cancel_emergency", boom):
            self.assertTrue(sim.service.cancel(incident.id, "Robin"))
        self.assertEqual(sim.telephony.hung_up, ["CA0001"])
        with mock.patch.object(sim.telephony, "hangup", boom), \
                mock.patch.object(sim.notifier, "notify_sender", boom):
            second = sim.trigger()
            self.assertTrue(sim.service.acknowledge(second.id, via="pushover"))
        self.assertEqual(sim.store.get(second.id).status, ACKNOWLEDGED)
        self.assertEqual(sim.notifier.cancelled, ["rcpt-" + second.id])

    def test_a_delayed_push_that_lands_after_the_acknowledgment_is_taken_back(self):
        sim = make(first_arm="call")
        incident = sim.trigger()
        real_send = sim.notifier.send_emergency

        def acknowledged_meanwhile(inc):
            receipt = real_send(inc)
            sim.service.acknowledge(inc.id, via="call")   # pressed 1 while the push was going out
            return receipt

        with mock.patch.object(sim.notifier, "send_emergency", acknowledged_meanwhile):
            self.assertEqual(sim.service.deliver_push(incident.id), "retracted")
        self.assertEqual(sim.notifier.cancelled, ["rcpt-" + incident.id])

    def test_a_pause_tells_the_sender_why_their_alert_stopped(self):
        sim = make()
        _, body = call("POST", "/trigger", {"kind": "kids"})
        call("POST", "/pause", {"minutes": 5}, token=ASSISTANT)
        _, view = call("GET", "/incidents/" + body["id"])
        self.assertEqual(view["final"],
                         "Alerts are paused, so this one was stopped. Reach the owner another way.")

    def test_a_cancel_by_the_sender_just_says_cancelled(self):
        make()
        _, body = call("POST", "/trigger", {"kind": "kids"})
        call("POST", "/incidents/%s/cancel" % body["id"])
        self.assertEqual(call("GET", "/incidents/" + body["id"])[1]["final"], "Cancelled.")

    def test_a_pause_never_lasts_more_than_a_day_whoever_asks(self):
        sim = make()
        self.assertEqual(sim.service.pause("anyone", 10 ** 9)["paused_seconds"], MAX_PAUSE_SECONDS)


class AbandonedIncident(Quiet):
    """The ticks are the only thing that keeps an incident alerting. If they
    stop, the incident must not sit there absorbing new alerts."""

    def test_an_incident_whose_ticks_stopped_is_replaced_by_the_next_trigger(self):
        sim = make()
        _, first = call("POST", "/trigger", {"kind": "kids"})
        sim.scheduler.queue.clear()          # the tick was parked after failing five times
        sim.clock.t += sim.cfg.abandoned_after + 1
        status, second = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual((status, second["created"]), (201, True))
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(len(sim.notifier.emergencies), 2)
        _, view = call("GET", "/incidents/" + first["id"])
        self.assertEqual(view["final"], "This alert stalled and was replaced by a new one.")

    def test_an_incident_that_is_still_ticking_is_joined_however_old_it_is(self):
        sim = make(primary_attempts=20, cycle_seconds=600)
        _, first = call("POST", "/trigger", {"kind": "kids"})
        for _ in range(6):                   # an hour in, ticks arriving on time
            due, _, incident_id, seq = sim.scheduler.queue.pop(0)
            sim.clock.t = due
            sim.service.tick(incident_id, seq)
            sim.service.call_status(incident_id, sim.telephony.calls[-1]["sid"], "no-answer")
        sim.clock.t += sim.cfg.cycle_seconds - 1
        status, again = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual((status, again["id"], again["created"]), (200, first["id"], False))

    def test_a_tick_that_is_only_late_is_not_taken_for_dead(self):
        sim = make()
        _, first = call("POST", "/trigger", {"kind": "kids"})
        sim.clock.t += sim.cfg.abandoned_after   # the queue redelivering, right at the limit
        self.assertEqual(call("POST", "/trigger", {"kind": "kids"})[1]["id"], first["id"])


class TestsNeverDisplaceRealAlerts(Quiet):
    def stalled_real_incident(self):
        sim = make()
        _, real = call("POST", "/trigger", {"kind": "kids"})
        sim.scheduler.queue.clear()
        sim.clock.t += sim.cfg.abandoned_after + 1
        return sim, real

    def test_a_drill_joins_a_stalled_real_alert_and_takes_nothing_back(self):
        sim, real = self.stalled_real_incident()
        status, drill = call("POST", "/trigger", {"kind": "kids", "test": True})
        self.assertEqual((status, drill["id"]), (200, real["id"]))
        self.assertEqual(sim.notifier.cancelled, [])       # Pushover may still be repeating it
        self.assertEqual(sim.store.get(real["id"]).status, ACTIVE)

    def test_a_self_test_leaves_a_stalled_real_alert_alone(self):
        sim, real = self.stalled_real_incident()
        status, body = call("POST", "/self-test", {}, token=ASSISTANT)
        self.assertEqual((status, body["reason"]), (409, "busy"))
        self.assertEqual(sim.notifier.cancelled, [])

    def test_the_real_alert_goes_out_before_the_drill_is_taken_back(self):
        sim = make(arm_offset_seconds=0)
        call("POST", "/trigger", {"kind": "kids", "test": True})
        sent_when_retracted = []
        retract = sim.notifier.cancel_emergency

        def note(receipt):
            sent_when_retracted.append((len(sim.notifier.emergencies), len(sim.telephony.calls)))
            retract(receipt)

        with mock.patch.object(sim.notifier, "cancel_emergency", note):
            call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(sent_when_retracted, [(2, 2)])

    def test_a_real_trigger_gets_through_even_if_another_drill_slips_in(self):
        sim = make()
        call("POST", "/trigger", {"kind": "kids", "test": True})
        create = sim.store.create_if_none_active
        calls = []

        def a_second_drill_wins_once(incident, now):
            calls.append(incident.test)
            if len(calls) == 2:          # the retry after closing the first drill
                sim.service.trigger("Robin", "kids", BASE, test=True)
            return create(incident, now)

        with mock.patch.object(sim.store, "create_if_none_active", a_second_drill_wins_once):
            status, real = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual((status, real["test"]), (201, False))

    def test_a_drill_whose_first_alert_fails_gets_an_ordinary_reply(self):
        # The Drill Shortcut's backup is a real alert, so it must not be set off.
        sim = make()
        with mock.patch.object(sim.notifier, "send_emergency", boom):
            status, body = call("POST", "/trigger", {"kind": "kids", "test": True})
        self.assertEqual(status, 201)
        self.assertIn("id", body)
        self.assertIn("Nothing has reached", body["summary"])

    def test_an_agents_self_test_whose_first_alert_fails_still_gets_its_fields(self):
        sim = make()
        with mock.patch.object(sim.notifier, "send_emergency", boom):
            status, body = call("POST", "/self-test", {}, token=ASSISTANT)
        self.assertEqual((status, body["started"]), (201, True))
        self.assertIn("Nothing has reached", body["summary"])

    def test_the_timer_self_test_raises_when_nothing_could_be_sent(self):
        sim = make(call_enabled=False, primary_number="")
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE}), \
                mock.patch.object(sim.notifier, "send_emergency", boom):
            with self.assertRaisesRegex(RuntimeError, "no alert could be sent"):
                handler.lambda_handler({"action": "selftest"})


class NothingReachedThePhone(Quiet):
    def test_a_second_press_gets_the_same_error_so_the_backup_fires_again(self):
        sim = make(call_enabled=False, primary_number="")
        with mock.patch.object(sim.notifier, "send_emergency", boom):
            first = call("POST", "/trigger", {"kind": "kids"})
            second = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual((first[0], second[0]), (502, 502))
        self.assertNotIn("id", second[1])
        self.assertEqual(second[1]["incident_id"], first[1]["incident_id"])

    def test_an_incident_that_never_sent_anything_is_replaced_after_a_minute(self):
        sim = make(call_enabled=False, primary_number="")
        with mock.patch.object(sim.notifier, "send_emergency", boom):
            _, first = call("POST", "/trigger", {"kind": "kids"})
        sim.scheduler.pushes.clear()             # the retry was parked
        sim.clock.t += 61
        status, second = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(status, 201)
        self.assertNotEqual(second["id"], first["incident_id"])
        self.assertEqual(len(sim.notifier.emergencies), 1)

    def test_a_slow_start_is_not_taken_for_dead(self):
        sim = make()                              # push now, first call at +30
        with mock.patch.object(sim.notifier, "send_emergency", boom):
            _, first = call("POST", "/trigger", {"kind": "kids"})
            sim.clock.t += 89                     # inside call delay + 60
            _, second = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(second["incident_id"], first["incident_id"])

    def test_a_stalled_alert_says_so_and_does_not_count_as_running(self):
        sim = make()
        _, real = call("POST", "/trigger", {"kind": "kids"})
        self.assertTrue(call("GET", "/break-glass/status", token=ASSISTANT)[1]["incident_active"])
        sim.scheduler.queue.clear()
        sim.clock.t += sim.cfg.abandoned_after + 1
        _, view = call("GET", "/incidents/" + real["id"])
        self.assertEqual(view["summary"], "This alert has stalled. Send it again, and reach the owner another way.")
        self.assertFalse(call("GET", "/break-glass/status", token=ASSISTANT)[1]["incident_active"])


class ClosedWhileSending(Quiet):
    def test_an_alert_sent_just_after_the_incident_closed_is_taken_back(self):
        sim = make(arm_offset_seconds=0)
        send = sim.notifier.send_emergency

        def cancelled_meanwhile(incident):
            sim.service.cancel(incident.id, "Robin")     # lands while the push is going out
            return send(incident)

        with mock.patch.object(sim.notifier, "send_emergency", cancelled_meanwhile):
            _, body = call("POST", "/trigger", {"kind": "kids"})
        incident = sim.store.get(body["id"])
        self.assertEqual(incident.status, CANCELLED)
        self.assertEqual(sim.notifier.cancelled, ["rcpt-" + incident.id])
        self.assertEqual(sim.telephony.hung_up, ["CA0001"])

    def test_a_call_placed_just_after_an_acknowledgment_is_hung_up(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "no-answer")
        sim.clock.t += sim.cfg.cycle_seconds
        place = sim.telephony.place_call

        def acknowledged_meanwhile(*args):
            sim.service.acknowledge(incident.id, via="pushover")
            return place(*args)

        with mock.patch.object(sim.telephony, "place_call", acknowledged_meanwhile):
            sim.service.tick(incident.id, 0)
        # The acknowledgment hung up the call it knew about; the one placed a
        # moment later is hung up when the tick reads the incident back.
        self.assertEqual(sim.telephony.hung_up[-1], "CA0002")


class CallsThatNeverConnected(Quiet):
    def test_a_call_twilio_reports_as_failed_is_not_counted_as_placed(self):
        sim = make(push_enabled=False, primary_attempts=2)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "failed")
        sim.service.call_status(incident.id, "CA0001", "failed")      # the callback, twice
        stored = sim.store.get(incident.id)
        self.assertEqual((stored.attempts, stored.calls_failed, stored.calls_placed), (1, 1, 0))
        sim.clock.t += sim.cfg.cycle_seconds
        sim.service.tick(incident.id, 0)
        self.assertEqual(sim.service.describe(sim.store.get(incident.id)),
                         "Still alerting the owner: no response yet, 1 call placed "
                         "(1 more could not be placed).")

    def test_busy_and_no_answer_reached_the_phone_and_count_as_placed(self):
        sim = make(push_enabled=False)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "busy")
        self.assertEqual(sim.store.get(incident.id).calls_failed, 0)


class LateKeypress(Quiet):
    def test_a_keypress_after_the_alert_ended_is_not_called_an_acknowledgment(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        sim.service.cancel(incident.id, "Robin")
        said = sim.service.ack_from_call(incident.id, "1")
        self.assertIn("nothing was acknowledged", said)
        self.assertEqual(sim.store.get(incident.id).status, CANCELLED)

    def test_a_keypress_delivered_twice_is_acknowledged_both_times(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        for _ in range(2):
            self.assertIn("Acknowledged. The calls will stop.", sim.service.ack_from_call(incident.id, "1"))
        self.assertEqual(len(sim.notifier.sender_messages), 1)   # the sender is told once


class Settings(unittest.TestCase):
    def test_settings_the_template_would_refuse_are_refused_here_too(self):
        for bad in ({"primary_attempts": 0}, {"backup_attempts": -1}, {"ring_timeout": 4},
                    {"ring_timeout": 61}):
            with self.assertRaises(ValueError, msg=bad):
                Config(primary_number=PRIMARY, **bad)

    def test_a_push_that_would_come_after_the_calls_have_ended_is_refused(self):
        with self.assertRaises(ValueError):
            Config(primary_number=PRIMARY, first_arm="call", primary_attempts=1,
                   cycle_seconds=60, arm_offset_seconds=60)

    def test_a_blank_backup_number_is_no_backup_number(self):
        config = Config(primary_number=PRIMARY, backup_number="  ")
        self.assertIsNone(config.backup_number)
        self.assertEqual(config.total_attempts, config.primary_attempts)

    def test_the_wait_for_a_silent_call_follows_the_ring_timeout(self):
        self.assertEqual(Config(primary_number=PRIMARY).call_grace, 125)
        self.assertEqual(Config(primary_number=PRIMARY, ring_timeout=60).call_grace, 160)

    def test_the_pointer_outlives_the_longest_incident(self):
        short = Config(primary_number=PRIMARY)
        self.assertEqual(short.pointer_ttl, short.active_pointer_ttl)
        long = Config(primary_number=PRIMARY, backup_number="+15550000002", cycle_seconds=900,
                      primary_attempts=9, backup_attempts=3)
        self.assertGreater(long.pointer_ttl, long.arm_offset_seconds + long.max_duration)

    def test_the_abandoned_limit_leaves_room_for_five_redeliveries(self):
        self.assertEqual(Config(primary_number=PRIMARY).abandoned_after, 390)
        self.assertEqual(Config(primary_number=PRIMARY, arm_offset_seconds=600).abandoned_after, 900)


class JunkFromStrangers(Quiet):
    """The URL is public. Junk must get a 4xx, never a 500 that trips the alarm."""

    def setUp(self):
        super().setUp()
        self.sim = make()

    def test_a_body_that_is_not_text(self):
        junk = base64.b64encode(b"\xff\xfe\x00 not utf-8").decode()
        self.assertEqual(call("POST", "/trigger", raw=junk, encoded=True, token=None)[0], 401)
        self.assertEqual(call("POST", "/trigger", raw=junk, encoded=True)[0], 400)

    def test_a_twilio_signature_that_is_not_ascii(self):
        incident = self.sim.trigger()
        status, _ = call("POST", "/twilio/voice/" + incident.id, token=None,
                         headers={"X-Twilio-Signature": "sígnature"}, raw="CallSid=CA1")
        self.assertEqual(status, 403)

    def test_a_pushover_receipt_that_is_not_ascii(self):
        incident = self.sim.trigger()
        status, body = call("POST", "/pushover/ack/" + incident.id, token=None,
                            raw=urllib.parse.urlencode({"acknowledged": "1", "receipt": "réceipt"}))
        self.assertEqual((status, body), (200, {"acknowledged": False}))

    def test_a_pause_for_infinity(self):
        status, _ = call("POST", "/pause", raw='{"minutes": Infinity}')
        self.assertEqual(status, 400)
        self.assertEqual(self.sim.service.paused_seconds(), 0)

    def test_control_characters_do_not_break_the_spoken_message(self):
        _, body = call("POST", "/trigger", {"kind": "ki\x01ds", "note": "line one\x0bline <two> &\x00three"})
        twiml = self.sim.service.voice_twiml(body["id"])
        spoken = xml.dom.minidom.parseString(twiml).getElementsByTagName("Say")[0].firstChild.data
        self.assertIn("Category: ki ds.", spoken)
        self.assertIn("line one line <two> & three", spoken)

    def test_half_a_location_or_one_off_the_map_is_dropped(self):
        for lat, lon in ((47.61, None), ("999", "10"), ("nan", "1"), (True, 2), (1e308, 1e308)):
            _, body = call("POST", "/trigger", {"kind": "kids", "lat": lat, "lon": lon})
            incident = self.sim.store.get(body["id"])
            self.assertEqual((incident.lat, incident.lon), (None, None), (lat, lon))
            self.sim.service.cancel(incident.id, "Robin")
        _, body = call("POST", "/trigger", {"kind": "kids", "lat": "47.61", "lon": -122.33})
        incident = self.sim.store.get(body["id"])
        self.assertEqual((incident.lat, incident.lon), ("47.610000", "-122.330000"))

    def test_a_break_glass_link_must_be_short_and_clean(self):
        long_url = "https://example.com/" + "a" * 600
        self.assertEqual(call("POST", "/break-glass", {"message": "m", "url": long_url},
                              token=ASSISTANT)[0], 400)
        self.assertEqual(call("POST", "/break-glass", {"message": "m", "url": "https://example.com/a b"},
                              token=ASSISTANT)[0], 400)
        self.assertEqual(len(self.sim.notifier.notices), 0)


class PublicBaseUrl(Quiet):
    def test_a_trailing_slash_does_not_break_twilio_signatures(self):
        sim = make()
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE + "/"}):
            _, body = call("POST", "/trigger", {"kind": "kids"})
            incident = sim.store.get(body["id"])
            self.assertEqual(incident.base_url, BASE)
            # Twilio signs the URL it was told to call.
            path = "/twilio/voice/" + incident.id
            fields = {"CallSid": "CA0001"}
            signature = compute_signature(TWILIO_TOKEN, incident.base_url + path, fields)
            status, twiml = call("POST", path, token=None, raw=urllib.parse.urlencode(fields),
                                 headers={"X-Twilio-Signature": signature})
        self.assertEqual(status, 200)
        self.assertIn("Press 1 to acknowledge", twiml)


class IdempotencyUnderFailure(Quiet):
    def send(self, key="evt-1"):
        return call("POST", "/break-glass", {"message": "m", "idempotency_key": key}, token=ASSISTANT)

    def test_a_key_abandoned_mid_request_can_be_used_again_after_a_minute(self):
        sim = make()
        sim.store.idem_begin("break-glass:Assistant:evt-1", sim.clock.now())   # the invocation died here
        self.assertEqual(self.send()[0], 409)
        sim.clock.t += 61
        self.assertEqual(self.send()[0], 200)
        self.assertEqual(len(sim.notifier.notices), 1)

    def test_a_delivered_alert_is_reported_as_delivered_even_if_the_record_fails(self):
        sim = make()
        with mock.patch.object(sim.store, "idem_finish", boom):
            status, body = self.send()
        self.assertEqual((status, body["delivered"]), (200, True))


class TokenLists(unittest.TestCase):
    def test_a_wrong_shape_is_refused_without_showing_name_or_token(self):
        with self.assertRaises(ValueError) as raised:
            Config(primary_number=PRIMARY, trigger_tokens={"Robin": 12345678})
        self.assertIn("trigger-tokens: entry 1", str(raised.exception))
        self.assertNotIn("12345678", str(raised.exception))
        with self.assertRaises(ValueError):
            Config(primary_number=PRIMARY, trigger_tokens=["not", "a", "map"])

    def test_a_list_written_token_to_name_is_refused_without_printing_the_token(self):
        with self.assertRaises(ValueError) as raised:
            Config(primary_number=PRIMARY, trigger_tokens={ROBIN: "Robin"})
        self.assertIn("name-to-token", str(raised.exception))
        self.assertNotIn(ROBIN, str(raised.exception))

    def test_a_broken_agent_list_cannot_stop_emergencies(self):
        with self.assertLogs("er", level="ERROR") as logs:
            config = Config(primary_number=PRIMARY, trigger_tokens={"Robin": ROBIN},
                            break_glass_tokens={"Assistant": ASSISTANT, "Broken": None, ROBIN + "x": "Jo"})
        self.assertEqual(config.break_glass_tokens, {"Assistant": ASSISTANT})
        self.assertEqual(config.trigger_tokens, {"Robin": ROBIN})
        self.assertNotIn(ROBIN, "".join(logs.output))

    def test_an_agent_list_that_is_not_json_cannot_stop_emergencies_either(self):
        with self.assertLogs("er", level="ERROR") as logs:
            config = Config.from_env(trigger_tokens=json.dumps({"Robin": ROBIN}),
                                     break_glass_tokens="{", env={"PRIMARY_NUMBER": PRIMARY})
        self.assertEqual(config.break_glass_tokens, {})
        self.assertIn("break-glass-tokens is not JSON", logs.output[0])
        with self.assertRaises(ValueError):       # the senders' list is another matter
            Config.from_env(trigger_tokens="{", env={"PRIMARY_NUMBER": PRIMARY})

    def test_a_sender_list_nobody_can_use_is_logged(self):
        with self.assertLogs("er", level="ERROR") as logs:
            Config(primary_number=PRIMARY, trigger_tokens={"Robin": ""})
        self.assertIn("nobody can start an emergency", logs.output[0])

    def test_unreadable_sender_receipt_keys_are_ignored_not_fatal(self):
        # What a deploy tool leaves when it cuts JSON at the first quote.
        for value in ("{", '["a"]', '{"Robin": 5}'):
            with self.assertLogs("er", level="ERROR"):
                config = Config.from_env(trigger_tokens=json.dumps({"Robin": ROBIN}),
                                         env={"PRIMARY_NUMBER": PRIMARY, "SENDER_PUSHOVER_KEYS": value})
            self.assertEqual(config.sender_pushover_keys, {}, value)
        config = Config.from_env(trigger_tokens=json.dumps({"Robin": ROBIN}),
                                 env={"PRIMARY_NUMBER": PRIMARY, "SENDER_PUSHOVER_KEYS": '{"Robin": "ukey"}'})
        self.assertEqual(config.sender_pushover_keys, {"Robin": "ukey"})

    def test_a_token_shared_between_the_lists_is_logged_as_an_error(self):
        with self.assertLogs("er", level="ERROR") as logs:
            Config(primary_number=PRIMARY, trigger_tokens={"Robin": ROBIN},
                   break_glass_tokens={"Assistant": ROBIN})
        self.assertIn("Assistant", logs.output[0])
        self.assertNotIn(ROBIN, logs.output[0])

    def test_a_short_token_is_warned_about_and_still_works(self):
        with self.assertLogs("er", level="WARNING") as logs:
            config = Config(primary_number=PRIMARY, trigger_tokens={"Robin": "short"})
        self.assertIn("Robin", logs.output[0])
        self.assertNotIn("short enough to guess: short", logs.output[0])
        self.assertEqual(config.trigger_tokens["Robin"], "short")


class SideBySide(unittest.TestCase):
    def test_results_come_back_in_order_and_errors_are_returned_not_raised(self):
        outcomes = side_by_side([lambda: 1, boom, lambda: 3])
        self.assertEqual([value for value, _ in outcomes], [1, None, 3])
        self.assertIsInstance(outcomes[1][1], RuntimeError)
        self.assertEqual(side_by_side([]), [])

    def test_a_job_that_dies_with_something_other_than_an_exception_is_still_an_error(self):
        def exits():
            raise SystemExit(3)

        outcomes = side_by_side([lambda: 1, exits])
        self.assertEqual(outcomes[0], (1, None))
        self.assertIsInstance(outcomes[1][1], SystemExit)
        self.assertIsInstance(side_by_side([exits])[0][1], SystemExit)

    def test_jobs_really_overlap(self):
        import threading
        barrier = threading.Barrier(2, timeout=5)
        # Each job waits for the other: run one after the other, this would time out.
        outcomes = side_by_side([barrier.wait, barrier.wait])
        self.assertEqual([error for _, error in outcomes], [None, None])


if __name__ == "__main__":
    unittest.main()
