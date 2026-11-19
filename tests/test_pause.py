"""The kill switch: pause stops everything and always ends by itself."""
import json
import unittest

from support import ASSISTANT, ROBIN, call, make, request


class Pause(unittest.TestCase):
    def setUp(self):
        self.sim = make()

    def test_needs_a_token(self):
        self.assertEqual(call("POST", "/pause", token="nope")[0], 401)
        self.assertEqual(call("POST", "/resume", token="nope")[0], 401)

    def test_pause_stops_the_active_incident(self):
        _, started = call("POST", "/trigger", {"kind": "kids"})
        self.sim.clock.t += 40  # the first call is ringing
        status, body = call("POST", "/pause", token=ASSISTANT)
        self.assertEqual(status, 200)
        self.assertEqual(body["stopped_incident"], started["id"])
        self.assertEqual(body["paused_seconds"], 3600)
        incident = self.sim.store.get(started["id"])
        self.assertEqual(incident.status, "cancelled")
        self.assertEqual(self.sim.notifier.cancelled, [incident.pushover_receipt])
        calls_before = len(self.sim.telephony.calls)
        self.sim.run()
        self.assertEqual(len(self.sim.telephony.calls), calls_before)
        self.assertIn("paused by Assistant", self.sim.notifier.sender_messages[-1][1])

    def test_trigger_is_refused_while_paused(self):
        call("POST", "/pause", {"minutes": 10})
        refused = request("POST", "/trigger", body={"kind": "kids"}, token=ROBIN)
        self.assertEqual(refused["statusCode"], 503)
        self.assertEqual(json.loads(refused["body"])["error"], "paused")
        self.assertEqual(refused["headers"]["retry-after"], "600")
        self.assertEqual(self.sim.notifier.emergencies, [])
        self.assertEqual(self.sim.telephony.calls, [])

    def test_break_glass_is_refused_while_paused(self):
        call("POST", "/pause", {"minutes": 10})
        status, body = call("POST", "/break-glass", {"message": "hi"}, token=ASSISTANT)
        self.assertEqual((status, body["reason"], body["retry_after_seconds"]), (429, "paused", 600))
        self.assertEqual(self.sim.notifier.notices, [])
        status, body = call("GET", "/break-glass/status", token=ASSISTANT)
        self.assertEqual((body["reason"], body["paused_seconds"]), ("paused", 600))
        self.assertEqual(body["paused_by"], "Robin")
        self.assertEqual(body["paused_until"], self.sim.clock.now() + 600)

    def test_pause_ends_by_itself(self):
        call("POST", "/pause", {"minutes": 10})
        self.sim.clock.t += 601
        self.assertEqual(call("POST", "/trigger", {"kind": "kids"})[0], 201)
        self.assertNotIn("paused_seconds", call("GET", "/break-glass/status", token=ASSISTANT)[1])

    def test_pause_is_capped_at_a_day(self):
        self.assertEqual(call("POST", "/pause", {"minutes": 99999})[1]["paused_seconds"], 86400)
        self.assertEqual(call("POST", "/pause", {"minutes": "x"})[0], 400)

    def test_resume(self):
        call("POST", "/pause")
        self.assertEqual(call("POST", "/resume", token=ASSISTANT)[1], {"paused": False})
        self.assertEqual(call("POST", "/trigger", {"kind": "kids"})[0], 201)

    def test_incident_that_slipped_in_is_stopped_at_its_next_tick(self):
        _, started = call("POST", "/trigger", {"kind": "kids"})
        self.sim.store.set_pause(self.sim.clock.now() + 600)
        self.assertEqual(self.sim.service.tick(started["id"], 0), "paused")
        self.assertEqual(self.sim.store.get(started["id"]).status, "cancelled")


if __name__ == "__main__":
    unittest.main()
