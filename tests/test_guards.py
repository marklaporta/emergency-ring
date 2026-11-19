"""Small rules that are easy to break without any other test noticing.

Each of these was found by changing the code slightly and seeing that the
suite still passed. They are cheap, and each guards something that matters:
who gets in, what a test may do, and what the owner or sender is told.
"""
import json
import unittest
from unittest import mock

from support import ASSISTANT, BASE, PRIMARY, ROBIN, call, make, request

from er.aws_adapters import DynamoStore
from er.config import PUSHOVER_MAX_EXPIRE, Config
from er.fakes import InMemoryStore
from er.models import CANCELLED, EXPIRED
from test_aws_adapters import TABLE, StubResource


class WhoGetsIn(unittest.TestCase):
    def setUp(self):
        self.sim = make()

    def status(self, authorization):
        response = request("POST", "/trigger", body={"kind": "kids"},
                           headers={"Authorization": authorization})
        return response["statusCode"]

    def test_only_a_bearer_token_counts(self):
        for header in ("Basic " + ROBIN, "Token " + ROBIN, ROBIN, "Bearer", "Bearer  "):
            self.assertEqual(self.status(header), 401, header)
        self.assertEqual(self.status("bearer " + ROBIN), 201)      # the scheme's case does not matter

    def test_part_of_a_token_or_a_token_with_more_on_the_end_is_not_the_token(self):
        for token in (ROBIN[:-1], ROBIN[:10], ROBIN + "x", ROBIN.upper(), " "):
            self.assertEqual(self.status("Bearer " + token), 401)
        self.assertEqual(len(self.sim.notifier.emergencies), 0)

    def test_an_incident_id_has_a_fixed_shape(self):
        for bad in ("short", "a" * 41, "has.dots.in.it", "has%20space", "../trigger"):
            self.assertEqual(call("GET", "/incidents/" + bad)[0], 404, bad)
        self.assertEqual(call("GET", "/incidents/" + "a" * 8)[0], 404)   # well formed, unknown


class WhatATestIs(unittest.TestCase):
    def test_a_rehearsal_is_a_test_with_the_real_sound(self):
        sim = make()
        _, body = call("POST", "/trigger", {"kind": "kids", "test": "full"})
        incident = sim.store.get(body["id"])
        self.assertEqual((incident.test, incident.rehearsal), (True, True))
        self.assertIn("This is a test", sim.service.voice_twiml(incident.id))

    def test_an_agent_gets_one_test_alert_a_day_not_one_a_window(self):
        sim = make()
        send = lambda: call("POST", "/break-glass", {"message": "m", "test": True}, token=ASSISTANT)[0]
        self.assertEqual(send(), 200)
        sim.clock.t += sim.cfg.break_glass_interval_seconds + 1
        self.assertEqual(send(), 429)
        sim.clock.t += 24 * 3600
        self.assertEqual(send(), 200)

    def test_a_refused_alert_can_be_sent_under_the_same_key_once_the_window_opens(self):
        sim = make()
        send = lambda: call("POST", "/break-glass", {"message": "m", "idempotency_key": "k"},
                            token=ASSISTANT)
        call("POST", "/break-glass", {"message": "first"}, token=ASSISTANT)
        self.assertEqual(send()[0], 429)
        sim.clock.t += sim.cfg.break_glass_interval_seconds + 1
        self.assertEqual(send(), (200, {"delivered": True, "retry_after_seconds": 900}))
        self.assertEqual(len(sim.notifier.notices), 2)


class WhatPeopleAreTold(unittest.TestCase):
    def test_the_wait_reported_while_paused_is_the_longer_of_pause_and_window(self):
        sim = make()
        call("POST", "/break-glass", {"message": "m"}, token=ASSISTANT)      # window: 15 minutes
        call("POST", "/pause", {"minutes": 5}, token=ASSISTANT)
        _, status = call("GET", "/break-glass/status", token=ASSISTANT)
        self.assertEqual((status["reason"], status["paused_seconds"]), ("paused", 300))
        self.assertEqual(status["retry_after_seconds"], 900)

    def test_a_pointer_left_on_a_closed_incident_does_not_count_as_active(self):
        sim = make()
        _, body = call("POST", "/trigger", {"kind": "kids"})
        sim.service.acknowledge(body["id"], via="call")
        sim.store.active_id_ = body["id"]            # its release never ran
        self.assertFalse(call("GET", "/break-glass/status", token=ASSISTANT)[1]["incident_active"])
        self.assertIsNone(call("POST", "/pause", {"minutes": 1})[1]["stopped_incident"])

    def test_an_incident_that_could_not_be_started_says_so(self):
        sim = make()

        def queue_down(*args):
            raise RuntimeError("queue unavailable")

        with mock.patch.object(sim.scheduler, "schedule_tick", queue_down), mock.patch("er.core.log"), \
                mock.patch("er.handler.log"):
            self.assertEqual(call("POST", "/trigger", {"kind": "kids"})[0], 500)
        (record,) = sim.store.items.values()
        self.assertEqual((record["status"], record["cancel_reason"]), (CANCELLED, "failed"))
        _, view = call("GET", "/incidents/" + record["id"])
        self.assertEqual(view["final"], "The alert could not be started. Reach the owner another way.")


class Hangups(unittest.TestCase):
    def test_an_acknowledgment_in_the_app_hangs_up_only_a_call_that_is_ringing(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "no-answer")       # that call is over
        sim.service.acknowledge(incident.id, via="pushover")
        self.assertEqual(sim.telephony.hung_up, [])

    def test_expiry_hangs_up_nothing(self):
        sim = make(arm_offset_seconds=0, primary_attempts=1, push_enabled=False)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)
        self.assertEqual(sim.telephony.hung_up, [])


class Arithmetic(unittest.TestCase):
    def test_a_pause_lasts_at_least_a_second(self):
        sim = make()
        for asked in (0, -5):
            self.assertEqual(sim.service.pause("Robin", asked)["paused_seconds"], 1)

    def test_the_callback_address_never_ends_in_a_slash(self):
        sim = make()
        incident, _ = sim.service.trigger("Robin", "kids", BASE + "/")
        self.assertEqual(incident.base_url, BASE)

    def test_with_one_arm_nothing_is_delayed(self):
        for arms in ({"call_enabled": False, "primary_number": ""}, {"push_enabled": False}):
            for first in ("push", "call"):
                config = Config(primary_number=arms.get("primary_number", PRIMARY), first_arm=first,
                                **{k: v for k, v in arms.items() if k != "primary_number"})
                self.assertEqual((config.call_delay, config.push_delay), (0, 0))

    def test_the_push_outlives_the_incident_but_not_pushovers_limit(self):
        default = Config(primary_number=PRIMARY)
        self.assertEqual(default.pushover_expire, 5 * 90 + 30 + 2 * 90)
        long = Config(primary_number=PRIMARY, cycle_seconds=900, primary_attempts=20)
        self.assertEqual(long.pushover_expire, PUSHOVER_MAX_EXPIRE)

    def test_a_call_in_progress_is_looked_at_again_soon(self):
        self.assertEqual(Config(primary_number=PRIMARY).busy_recheck_seconds, 15)


class UpdatesOnlyWhatExists(unittest.TestCase):
    """update_item would create a half-empty record that cannot be read back."""

    def test_the_real_store(self):
        store = DynamoStore(TABLE, Config(primary_number=PRIMARY), resource=StubResource())
        store.update("incident-gone", last_call_sid="CA1")
        self.assertIsNone(store.get("incident-gone"))

    def test_the_fake(self):
        store = InMemoryStore()
        store.update("incident-gone", last_call_sid="CA1")
        self.assertIsNone(store.get("incident-gone"))


class DuplicateQueuedPush(unittest.TestCase):
    def test_a_copy_that_lost_the_race_takes_its_own_alert_back(self):
        sim = make(first_arm="call")
        incident = sim.trigger()
        send = sim.notifier.send_emergency

        def another_copy_stores_its_receipt_first(inc):
            receipt = send(inc)
            real_update = sim.store.update
            def update(incident_id, **fields):
                real_update(incident_id, **fields)
                if "pushover_receipt" in fields:
                    real_update(incident_id, pushover_receipt="rcpt-from-the-other-copy")
            sim.store.update = update
            return receipt

        with mock.patch.object(sim.notifier, "send_emergency", another_copy_stores_its_receipt_first):
            self.assertEqual(sim.service.deliver_push(incident.id), "retracted")
        self.assertEqual(sim.notifier.cancelled, ["rcpt-" + incident.id])


if __name__ == "__main__":
    unittest.main()
