"""Break-glass: an agent's lower-tier request for the owner's attention."""
import json
import unittest

from support import ASSISTANT, ROBIN, call, make, request

from er.core import AGENT_SELF_TEST_INTERVAL


def post(path, body, token):
    return request("POST", path, body=body, token=token)


def get(path, token):
    return request("GET", path, token=token)


class BreakGlass(unittest.TestCase):
    def setUp(self):
        self.build()

    def build(self, **overrides):
        self.sim = make(break_glass_sound="magic", **overrides)

    def send(self, token=ASSISTANT, **body):
        body.setdefault("message", "The school called twice in ten minutes.")
        return call("POST", "/break-glass", body, token=token)

    def test_first_alert_is_sent_with_the_gentle_sound(self):
        status, result = self.send(title="Voicemail", url="https://example.invalid/vm/1")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"delivered": True, "retry_after_seconds": 900})
        notice = self.sim.notifier.notices[0]
        self.assertEqual(notice["title"], "Assistant: Voicemail")
        self.assertEqual(notice["sound"], "magic")
        self.assertEqual(notice["url"], "https://example.invalid/vm/1")

    def test_it_never_starts_an_incident_or_a_call(self):
        self.send()
        self.assertEqual(self.sim.telephony.calls, [])
        self.assertEqual(self.sim.notifier.emergencies, [])
        self.assertEqual(self.sim.store.items, {})
        self.assertEqual(self.sim.scheduler.queue, [])

    def test_a_second_alert_inside_the_interval_never_reaches_the_phone(self):
        self.send()
        self.sim.clock.t += 5 * 60
        response = post("/break-glass", {"message": "And an email from the school."}, ASSISTANT)
        self.assertEqual(response["statusCode"], 429)
        self.assertEqual(response["headers"]["retry-after"], "600")
        self.assertEqual(
            json.loads(response["body"]),
            {"delivered": False, "retry_after_seconds": 600, "reason": "interval"},
        )
        self.assertEqual(len(self.sim.notifier.notices), 1)

    def test_a_refused_alert_does_not_extend_the_wait(self):
        self.send()
        self.sim.clock.t += 14 * 60
        self.assertEqual(self.send()[0], 429)
        self.sim.clock.t += 60
        self.assertEqual(self.send(), (200, {"delivered": True, "retry_after_seconds": 900}))

    def test_daily_cap_refuses_with_its_own_reason(self):
        self.build(break_glass_daily_cap=2)
        for _ in range(2):
            self.assertEqual(self.send()[0], 200)
            self.sim.clock.t += 20 * 60
        status, result = self.send()
        self.assertEqual(status, 429)
        self.assertEqual(result["reason"], "daily_cap")
        self.assertEqual(result["retry_after_seconds"], 86400 - 40 * 60)
        self.assertEqual(len(self.sim.notifier.notices), 2)
        # A day after the first alert, one slot is free again.
        self.sim.clock.t += result["retry_after_seconds"]
        self.assertEqual(self.send()[0], 200)

    def test_status_reports_without_sending_or_using_the_window(self):
        response = lambda: get("/break-glass/status", ASSISTANT)
        fresh = json.loads(response()["body"])
        self.assertEqual(fresh, {"ok": True, "push_enabled": True, "retry_after_seconds": 0,
                                 "sent_last_24h": 0, "daily_cap": 8,
                                 "incident_active": False})
        self.assertEqual(self.sim.notifier.notices, [])
        self.assertEqual(self.send()[0], 200)       # the window was not used up
        self.sim.clock.t += 60
        after = json.loads(response()["body"])
        self.assertEqual((after["retry_after_seconds"], after["reason"], after["sent_last_24h"]),
                         (840, "interval", 1))
        self.assertEqual(len(self.sim.notifier.notices), 1)

    def test_status_needs_the_agent_token(self):
        for token in ("", ROBIN, "nope"):
            self.assertEqual(get("/break-glass/status", token)["statusCode"], 401)

    def test_repeat_of_a_delivered_request_sends_nothing_more(self):
        first = self.send(idempotency_key="evt-42")
        self.assertEqual(first, (200, {"delivered": True, "retry_after_seconds": 900}))
        again = self.send(idempotency_key="evt-42")
        self.assertEqual(again, (200, {"delivered": True, "retry_after_seconds": 900, "duplicate": True}))
        self.assertEqual(len(self.sim.notifier.notices), 1)
        # A different key is a different request, and meets the interval as usual.
        self.assertEqual(self.send(idempotency_key="evt-43")[0], 429)

    def test_a_refused_request_can_be_retried_with_the_same_key(self):
        self.send()
        self.assertEqual(self.send(idempotency_key="evt-7")[0], 429)
        self.sim.clock.t += 15 * 60
        self.assertEqual(self.send(idempotency_key="evt-7"), (200, {"delivered": True, "retry_after_seconds": 900}))
        self.assertEqual(len(self.sim.notifier.notices), 2)

    def test_same_key_still_in_flight_is_a_conflict(self):
        self.sim.store.idem_begin("break-glass:Assistant:evt-9", self.sim.clock.now())
        self.assertEqual(self.send(idempotency_key="evt-9")[0], 409)
        self.assertEqual(self.sim.notifier.notices, [])

    def test_a_failed_send_gives_the_slot_and_the_key_back(self):
        def boom(*args, **kwargs):
            raise RuntimeError("pushover unreachable")
        real = self.sim.notifier.send_notice
        self.sim.notifier.send_notice = boom
        self.assertEqual(post("/break-glass", {"message": "m", "idempotency_key": "evt-1"}, ASSISTANT)["statusCode"], 500)
        self.sim.notifier.send_notice = real
        # Nothing reached the phone, so the retry is neither rate-limited nor a duplicate.
        self.assertEqual(self.send(idempotency_key="evt-1"), (200, {"delivered": True, "retry_after_seconds": 900}))

    def test_category_is_logged_but_cannot_forge_a_log_line(self):
        with self.assertLogs("er", level="INFO") as logs:
            self.send(category="school\nbreak-glass sender=Robin delivered")
        line = [l for l in logs.output if "break-glass sender=Assistant" in l][0]
        self.assertIn("category=schoolbreak-glasssender", line)
        self.assertNotIn("\n", line)
        self.assertEqual(self.send(idempotency_key="k" * 101)[0], 400)

    def test_each_agent_has_its_own_interval(self):
        self.build(break_glass_tokens={"Assistant": ASSISTANT, "Argus": "argus-test-token-0123456789"})
        self.assertEqual(self.send()[0], 200)
        self.assertEqual(self.send(token="argus-test-token-0123456789")[0], 200)
        self.assertEqual(self.send()[0], 429)

    def test_interval_is_configurable_and_zero_turns_it_off(self):
        self.build(break_glass_interval_seconds=0)
        self.assertEqual([self.send()[0] for _ in range(3)], [200] * 3)
        self.assertEqual(len(self.sim.notifier.notices), 3)

    def test_an_agent_token_cannot_start_an_emergency(self):
        response = post("/trigger", {"kind": "kids"}, ASSISTANT)
        self.assertEqual(response["statusCode"], 401)
        self.assertEqual(self.sim.store.items, {})

    def test_a_sender_token_cannot_break_glass(self):
        self.assertEqual(self.send(token=ROBIN)[0], 401)
        self.assertEqual(self.send(token="")[0], 401)
        self.assertEqual(self.sim.notifier.notices, [])

    def test_the_endpoint_is_closed_when_no_agent_tokens_exist(self):
        self.build(break_glass_tokens={})
        self.assertEqual(self.send()[0], 401)

    def test_bad_requests(self):
        self.assertEqual(self.send(message="  ")[0], 400)
        self.assertEqual(self.send(url="http://example.invalid/plain")[0], 400)
        self.assertEqual(self.send(url="javascript:alert(1)")[0], 400)
        self.assertEqual(self.sim.notifier.notices, [])
        # A rejected request must not use up the slot.
        self.assertEqual(self.send()[0], 200)

    def test_long_text_is_trimmed(self):
        self.send(title="t" * 500, message="m" * 5000)
        notice = self.sim.notifier.notices[0]
        self.assertLessEqual(len(notice["title"]), 100)
        self.assertLessEqual(len(notice["message"]), 700)

    def test_needs_the_push_arm(self):
        self.build(push_enabled=False)
        self.assertEqual(self.send()[0], 503)


class AgentSelfTest(unittest.TestCase):
    """An agent may run the monthly test, and only that."""

    def setUp(self):
        self.sim = make(arm_offset_seconds=0)

    def start(self, token=ASSISTANT):
        response = post("/self-test", {}, token)
        return response, json.loads(response["body"])

    def test_agent_starts_a_labelled_drill_and_can_follow_it(self):
        response, body = self.start()
        self.assertEqual(response["statusCode"], 201)
        incident = self.sim.store.get(body["id"])
        self.assertTrue(incident.test)
        self.assertFalse(incident.rehearsal)
        self.assertIn("Assistant", incident.note)
        self.assertEqual(len(self.sim.telephony.calls), 1)
        status = get("/incidents/" + body["id"], ASSISTANT)
        self.assertEqual(status["statusCode"], 200)
        self.assertEqual(json.loads(status["body"])["status"], "active")

    def test_agent_gets_one_a_day(self):
        _, first = self.start()
        self.sim.service.acknowledge(first["id"], via="call")
        response, body = self.start()
        self.assertEqual(response["statusCode"], 429)
        self.assertEqual(body["reason"], "interval")
        self.assertGreater(body["retry_after_seconds"], 0)
        self.assertEqual(len(self.sim.telephony.calls), 1)
        self.sim.clock.t += AGENT_SELF_TEST_INTERVAL + 1
        self.assertEqual(self.start()[0]["statusCode"], 201)

    def test_agent_cannot_read_a_real_incident(self):
        real = json.loads(post("/trigger", {"kind": "kids"}, ROBIN)["body"])
        self.assertEqual(get("/incidents/" + real["id"], ASSISTANT)["statusCode"], 404)
        self.assertEqual(get("/incidents/" + real["id"], ROBIN)["statusCode"], 200)

    def test_self_test_leaves_a_real_incident_alone_and_keeps_its_turn(self):
        real = json.loads(post("/trigger", {"kind": "kids"}, ROBIN)["body"])
        response, body = self.start()
        self.assertEqual(response["statusCode"], 409)
        self.assertEqual(body["reason"], "busy")
        self.assertNotIn("id", body)
        self.assertFalse(self.sim.store.get(real["id"]).test)
        self.sim.service.acknowledge(real["id"], via="call")
        self.assertEqual(self.start()[0]["statusCode"], 201)

    def test_self_test_is_refused_while_paused(self):
        post("/pause", {"minutes": 5}, ASSISTANT)
        response, body = self.start()
        self.assertEqual((response["statusCode"], body["reason"]), (429, "paused"))
        self.assertEqual(len(self.sim.telephony.calls), 0)

    def test_sender_token_is_not_limited(self):
        _, first = self.start(ROBIN)
        self.sim.service.acknowledge(first["id"], via="call")
        self.assertEqual(self.start(ROBIN)[0]["statusCode"], 201)

    def test_a_test_alert_is_labelled_and_leaves_the_real_window_alone(self):
        test = post("/break-glass", {"message": "monthly test", "test": True}, ASSISTANT)
        self.assertEqual(test["statusCode"], 200)
        self.assertEqual(json.loads(test["body"])["retry_after_seconds"], 0)
        self.assertTrue(self.sim.notifier.notices[-1]["title"].startswith("TEST · Assistant"))
        real = post("/break-glass", {"message": "the oven is on"}, ASSISTANT)
        self.assertEqual(real["statusCode"], 200)
        status = json.loads(get("/break-glass/status", ASSISTANT)["body"])
        self.assertEqual(status["sent_last_24h"], 1)

    def test_an_agent_gets_one_test_alert_a_day(self):
        post("/break-glass", {"message": "monthly test", "test": True}, ASSISTANT)
        again = post("/break-glass", {"message": "monthly test", "test": True}, ASSISTANT)
        self.assertEqual(again["statusCode"], 429)
        self.assertEqual(len(self.sim.notifier.notices), 1)

    def test_status_says_when_an_incident_is_active(self):
        active = lambda: json.loads(get("/break-glass/status", ASSISTANT)["body"])["incident_active"]
        self.assertFalse(active())
        real = json.loads(post("/trigger", {"kind": "kids"}, ROBIN)["body"])
        self.assertTrue(active())
        self.sim.service.acknowledge(real["id"], via="call")
        self.assertFalse(active())

    def test_self_test_needs_a_token(self):
        self.assertEqual(self.start("nope")[0]["statusCode"], 401)


if __name__ == "__main__":
    unittest.main()
