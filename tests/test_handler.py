import json
import os
import unittest
import urllib.parse
from unittest import mock

from support import ASSISTANT, BASE, PRIMARY, ROBIN, TWILIO_TOKEN, call, event, make, request

from er import handler
from er.core import SELF_TEST_SENDER
from er.models import ACKNOWLEDGED
from er.twilio_adapter import compute_signature


def twilio(path, fields, token=TWILIO_TOKEN):
    signature = compute_signature(token, BASE + path, fields)
    return event("POST", path, raw=urllib.parse.urlencode(fields),
                 headers={"X-Twilio-Signature": signature})


class HandlerTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PUBLIC_BASE_URL", None)
        self.sim = make(arm_offset_seconds=0, cycle_seconds=60)

    def trigger(self, token=ROBIN, body=None):
        if body is None:
            body = {"kind": "kids", "lat": 47.61, "lon": -122.33}
        return request("POST", "/trigger", body=body, token=token)

    def test_trigger_requires_valid_token(self):
        self.assertEqual(self.trigger(token="nope")["statusCode"], 401)
        self.assertEqual(self.trigger(token="")["statusCode"], 401)
        self.assertEqual(len(self.sim.telephony.calls), 0)

    def test_trigger_creates_incident_and_names_sender_from_token(self):
        response = self.trigger(body={"kind": "kids", "sender": "Mallory"})
        self.assertEqual(response["statusCode"], 201)
        incident_id = json.loads(response["body"])["id"]
        incident = self.sim.store.get(incident_id)
        self.assertEqual(incident.sender, "Robin")
        self.assertEqual(incident.base_url, BASE)
        self.assertEqual(len(self.sim.telephony.calls), 1)

    def test_scheduled_self_test_starts_a_labelled_drill(self):
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE}):
            result = handler.lambda_handler({"action": "selftest"})
        incident = self.sim.store.get(result["incident"])
        self.assertTrue(incident.test)
        self.assertFalse(incident.rehearsal)
        self.assertEqual((incident.sender, incident.base_url), (SELF_TEST_SENDER, BASE))
        self.assertEqual(len(self.sim.telephony.calls), 1)

    def test_scheduled_self_test_needs_the_public_url(self):
        with self.assertRaises(RuntimeError):
            handler.lambda_handler({"action": "selftest"})
        self.assertEqual(len(self.sim.telephony.calls), 0)

    def test_scheduled_self_test_stays_quiet_while_paused(self):
        self.sim.service.pause(by="Robin", seconds=600)
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE}):
            result = handler.lambda_handler({"action": "selftest"})
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(len(self.sim.telephony.calls), 0)

    def test_test_flag_marks_the_incident_as_a_drill(self):
        drill = json.loads(self.trigger(body={"kind": "kids", "test": True})["body"])
        self.assertTrue(self.sim.store.get(drill["id"]).test)
        self.assertIn("This is a test", self.sim.service.voice_twiml(drill["id"]))
        self.sim.service.acknowledge(drill["id"], via="call")
        for value, expected in (("drill", (True, False)), ("full", (True, True))):
            made = json.loads(self.trigger(body={"kind": "kids", "test": value})["body"])
            incident = self.sim.store.get(made["id"])
            self.assertEqual((incident.test, incident.rehearsal), expected, value)
            self.assertIn("This is a test", self.sim.service.voice_twiml(made["id"]))
            self.sim.service.acknowledge(made["id"], via="call")
        # Only these values count; a stray string must not downgrade a real alert.
        real = json.loads(self.trigger(body={"kind": "kids", "test": "false"})["body"])
        self.assertFalse(self.sim.store.get(real["id"]).test)
        self.assertIn("Emergency alert from Robin", self.sim.service.voice_twiml(real["id"]))

    def test_second_trigger_returns_200_and_same_incident(self):
        first = json.loads(self.trigger()["body"])
        second = self.trigger()
        self.assertEqual(second["statusCode"], 200)
        self.assertEqual(json.loads(second["body"])["id"], first["id"])

    def test_twilio_routes_reject_bad_signature(self):
        incident_id = json.loads(self.trigger()["body"])["id"]
        path = "/twilio/ack/" + incident_id
        forged = twilio(path, {"Digits": "1"}, token="wrong-token")
        self.assertEqual(handler.lambda_handler(forged)["statusCode"], 403)
        unsigned = event("POST", path, raw="Digits=1")
        self.assertEqual(handler.lambda_handler(unsigned)["statusCode"], 403)
        self.assertNotEqual(self.sim.store.get(incident_id).status, ACKNOWLEDGED)

    def test_signed_keypress_acknowledges(self):
        incident_id = json.loads(self.trigger()["body"])["id"]
        voice = handler.lambda_handler(twilio("/twilio/voice/" + incident_id, {"CallSid": "CA0001"}))
        self.assertEqual(voice["headers"]["content-type"], "text/xml")
        self.assertIn("Press 1 to acknowledge", voice["body"])
        ack = handler.lambda_handler(twilio("/twilio/ack/" + incident_id, {"Digits": "1"}))
        self.assertIn("Acknowledged", ack["body"])
        self.assertEqual(self.sim.store.get(incident_id).status, ACKNOWLEDGED)

    def test_status_and_cancel(self):
        incident_id = json.loads(self.trigger()["body"])["id"]
        self.assertEqual(call("GET", "/incidents/" + incident_id)[1]["status"], "active")
        self.assertEqual(call("GET", "/incidents/" + incident_id, token=None)[0], 401)
        self.assertTrue(call("POST", "/incidents/%s/cancel" % incident_id)[1]["cancelled"])

    def test_status_has_final_only_when_over(self):
        _, started = call("POST", "/trigger", {"kind": "kids"})
        path = "/incidents/" + started["id"]
        self.assertNotIn("final", call("GET", path)[1])
        call("POST", path + "/cancel")
        self.assertEqual(call("GET", path)[1]["final"], "Cancelled.")

    def test_pushover_callback_needs_matching_receipt(self):
        incident_id = json.loads(self.trigger()["body"])["id"]
        path = "/pushover/ack/" + incident_id
        bad = request("POST", path, raw="acknowledged=1&receipt=guess")
        self.assertFalse(json.loads(bad["body"])["acknowledged"])
        # Every field Pushover documents for the acknowledgment callback.
        fields = {
            "receipt": "rcpt-" + incident_id,
            "acknowledged": "1",
            "acknowledged_at": "1360019238",
            "acknowledged_by": "owner-user-key-example",
            "acknowledged_by_device": "iphone",
        }
        body = urllib.parse.urlencode(fields)
        # Right receipt, but Pushover does not confirm it: not acknowledged.
        unconfirmed = request("POST", path, raw=body)
        self.assertFalse(json.loads(unconfirmed["body"])["acknowledged"])
        # Pushover cannot be reached: answer 5xx so it retries the callback.
        self.sim.notifier.lookup_fails = True
        self.assertEqual(request("POST", path, raw=body)["statusCode"], 500)
        self.sim.notifier.lookup_fails = False

        self.sim.notifier.acknowledged.add("rcpt-" + incident_id)
        good = request("POST", path, raw=body)
        self.assertEqual(good["statusCode"], 200)
        self.assertTrue(json.loads(good["body"])["acknowledged"])
        self.assertEqual(self.sim.store.get(incident_id).acked_via, "pushover")

    def test_sqs_tick_event(self):
        incident_id = json.loads(self.trigger()["body"])["id"]
        self.sim.service.call_status(incident_id, "CA0001", "no-answer")
        self.sim.clock.t += 60
        event = {"Records": [{"body": json.dumps({"incident_id": incident_id, "seq": 0})}]}
        handler.lambda_handler(event)
        self.assertEqual(len(self.sim.telephony.calls), 2)

    def test_status_carries_a_sentence_the_sender_can_be_shown(self):
        def summary(incident_id):
            return call("GET", "/incidents/" + incident_id)[1]["summary"]

        made = json.loads(self.trigger()["body"])
        self.assertEqual(made["summary"], "Still alerting the owner: no response yet, 1 call placed.")
        self.sim.service.acknowledge(made["id"], via="call")
        self.assertEqual(summary(made["id"]), "The owner answered the call and acknowledged.")

        second = json.loads(self.trigger()["body"])["id"]
        self.sim.service.acknowledge(second, via="pushover")
        self.assertEqual(summary(second), "The owner acknowledged the alert.")

        third = json.loads(self.trigger()["body"])["id"]
        self.sim.service.cancel(third, "Robin")
        self.assertEqual(summary(third), "Cancelled.")

        fourth = json.loads(self.trigger()["body"])["id"]
        self.sim.run()
        self.assertIn("Nobody acknowledged", summary(fourth))

    def test_unknown_route(self):
        self.assertEqual(request("GET", "/")["statusCode"], 404)

    def test_twilio_signature_known_vector(self):
        # Example from Twilio's request-validation documentation.
        params = {
            "CallSid": "CA1234567890ABCDE",
            "Caller": "+12349013030",
            "Digits": "1234",
            "From": "+12349013030",
            "To": "+18005551212",
        }
        signature = compute_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params)
        self.assertEqual(signature, "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")

    def test_a_body_that_is_not_json_is_refused(self):
        for path, token in (("/trigger", ROBIN), ("/break-glass", ASSISTANT), ("/pause", ROBIN)):
            status, body = call("POST", path, raw="{not json", token=token)
            self.assertEqual((status, body), (400, {"error": "body must be JSON"}), path)
        self.assertEqual(self.sim.store.items, {})
        self.assertEqual(self.sim.notifier.notices, [])
        self.assertEqual(self.sim.service.paused_seconds(), 0)


class WhoMayCallWhat(unittest.TestCase):
    """Every token route against every kind of caller. An agent's token must
    never start or cancel an emergency, and a sender's never breaks glass."""

    # The status for: no header, an empty token, a wrong token, a sender's
    # token, an agent's token. ID stands for a real incident of Robin's.
    ROUTES = [
        ("POST", "/trigger", {"kind": "kids"}, (401, 401, 401, 201, 401)),
        ("POST", "/pause", None, (401, 401, 401, 200, 200)),
        ("POST", "/resume", None, (401, 401, 401, 200, 200)),
        ("POST", "/self-test", None, (401, 401, 401, 201, 201)),
        ("POST", "/break-glass", {"message": "m"}, (401, 401, 401, 401, 200)),
        ("GET", "/break-glass/status", None, (401, 401, 401, 401, 200)),
        ("GET", "/incidents/ID", None, (401, 401, 401, 200, 404)),
        ("POST", "/incidents/ID/cancel", None, (401, 401, 401, 200, 401)),
    ]

    def test_each_route_lets_in_only_the_callers_it_should(self):
        for method, path, body, expected in self.ROUTES:
            for token, status in zip((None, "", "nope", ROBIN, ASSISTANT), expected):
                sim = make(arm_offset_seconds=0)
                incident = sim.trigger()
                if path in ("/trigger", "/self-test"):
                    # Out of the way, so that a caller who is let in starts a new one.
                    sim.service.acknowledge(incident.id, via="call")
                got, _ = call(method, path.replace("ID", incident.id), body, token=token)
                self.assertEqual(got, status, (method, path, token))

    def test_a_refused_caller_changes_nothing(self):
        sim = make()
        self.assertEqual(call("POST", "/trigger", {"kind": "kids"}, token=ASSISTANT)[0], 401)
        self.assertEqual(call("POST", "/break-glass", {"message": "m"}, token=ROBIN)[0], 401)
        self.assertEqual(call("POST", "/pause", token="nope")[0], 401)
        self.assertEqual((sim.store.items, sim.notifier.notices), ({}, []))
        self.assertEqual(sim.service.paused_seconds(), 0)

    def test_a_token_on_both_lists_counts_as_the_sender_where_both_may_call(self):
        with self.assertLogs("er", level="ERROR"):
            make(break_glass_tokens={"Assistant": ROBIN})
        self.assertEqual(call("POST", "/pause")[1]["paused_by"], "Robin")


class BuildService(unittest.TestCase):
    """Cold-start wiring, with SSM, DynamoDB and SQS patched out."""

    ENV = {
        "SSM_PREFIX": "/emergency-ring",
        "TABLE_NAME": "incidents",
        "TICK_QUEUE_URL": "https://sqs.invalid/queue",
        "PRIMARY_NUMBER": PRIMARY,
        "TWILIO_ACCOUNT_SID": "AC123",
        "TWILIO_FROM_NUMBER": "+15550000009",
    }

    def build(self, secrets, env=None, required=("trigger-tokens", "twilio-auth-token",
                                                 "pushover-app-token", "pushover-user-key")):
        with mock.patch.dict(os.environ, dict(self.ENV, **(env or {}))), \
                mock.patch("er.ssm_secrets.load_secrets", return_value=secrets) as load, \
                mock.patch("er.aws_adapters.DynamoStore"), \
                mock.patch("er.aws_adapters.SqsScheduler"):
            service = handler.build_service()
        load.assert_called_once_with("/emergency-ring", required=required)
        return service, service.cfg

    def test_secrets_from_ssm_reach_config_twilio_and_pushover(self):
        service, config = self.build({
            "twilio-auth-token": TWILIO_TOKEN,
            "trigger-tokens": json.dumps({"Robin": ROBIN}),
            "pushover-app-token": "app-token",
            "pushover-user-key": "user-key",
        })
        self.assertEqual(config.trigger_tokens, {"Robin": ROBIN})
        self.assertEqual(config.twilio_auth_token, TWILIO_TOKEN)
        self.assertEqual(service.telephony.auth_token, TWILIO_TOKEN)
        self.assertEqual(service.notifier.app_token, "app-token")
        self.assertEqual(service.notifier.owner_user_key, "user-key")
        # The push repeats on the same cycle as the calls, with the alarm sound.
        self.assertEqual(service.notifier.retry, 90)
        self.assertEqual(service.notifier.sound, "persistent")
        self.assertEqual((config.first_arm, config.arm_offset_seconds, config.cycle_seconds),
                         ("push", 30, 90))

    def test_push_arm_off_is_calls_only_and_needs_no_pushover_secrets(self):
        service, config = self.build(
            {"twilio-auth-token": TWILIO_TOKEN, "trigger-tokens": json.dumps({"Robin": ROBIN})},
            env={"PUSH_ENABLED": "false"},
            required=("trigger-tokens", "twilio-auth-token"),
        )
        self.assertEqual(type(service.notifier).__name__, "NullNotifier")
        self.assertFalse(config.push_enabled)

    def test_call_arm_off_is_push_only_and_needs_no_twilio_settings(self):
        env = {"CALL_ENABLED": "false", "PRIMARY_NUMBER": "",
               "TWILIO_ACCOUNT_SID": "", "TWILIO_FROM_NUMBER": ""}
        service, config = self.build(
            {"trigger-tokens": json.dumps({"Robin": ROBIN}),
             "pushover-app-token": "app-token", "pushover-user-key": "user-key"},
            env=env,
            required=("trigger-tokens", "pushover-app-token", "pushover-user-key"),
        )
        self.assertEqual(type(service.telephony).__name__, "NullTelephony")
        self.assertFalse(config.call_enabled)
        self.assertEqual(config.twilio_auth_token, "")

    def test_settings_are_read_from_the_environment(self):
        _, config = self.build(
            {"twilio-auth-token": TWILIO_TOKEN, "trigger-tokens": "{}",
             "pushover-app-token": "a", "pushover-user-key": "u"},
            env={"FIRST_ARM": "call", "ARM_OFFSET_SECONDS": "45", "CYCLE_SECONDS": "120",
                 "PUSH_SOUND": "siren", "PRIMARY_ATTEMPTS": "4", "RING_TIMEOUT": "20"},
        )
        self.assertEqual((config.first_arm, config.arm_offset_seconds, config.cycle_seconds),
                         ("call", 45, 120))
        self.assertEqual((config.push_sound, config.primary_attempts, config.ring_timeout),
                         ("siren", 4, 20))

    def test_both_arms_off_is_refused(self):
        with self.assertRaises(ValueError):
            self.build(
                {"trigger-tokens": "{}"},
                env={"CALL_ENABLED": "false", "PUSH_ENABLED": "false"},
                required=("trigger-tokens",),
            )

    def test_call_arm_on_without_twilio_settings_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.build(
                {"twilio-auth-token": TWILIO_TOKEN, "trigger-tokens": "{}",
                 "pushover-app-token": "a", "pushover-user-key": "u"},
                env={"TWILIO_FROM_NUMBER": ""},
            )


if __name__ == "__main__":
    unittest.main()
