"""The corners of the adapters that the other test files leave alone.

Pushover requests for an alert with no note or location; the stand-in
adapters used when one arm is off; the delay on an SQS message; the secrets
loader's edges; and how the AWS clients are built when nobody passes one in.
No boto3 and no network: urlopen is replaced, and boto3 is a recording
stand-in.
"""
import json
import os
import socket
import subprocess
import sys
import types
import unittest
import urllib.error
from decimal import Decimal
from unittest import mock

import support
from support import PRIMARY
from test_aws_adapters import TABLE, StubResource
from test_store_contract import function_timeout
from test_twilio_adapter import RecordedHttp, http_error

from er import aws_adapters
from er.aws_adapters import DynamoStore, SqsScheduler, client_config
from er.config import MAX_DELAY, Config
from er.fakes import ACK, NO_ANSWER, Simulation
from er.models import ACKNOWLEDGED, CANCELLED, EXPIRED, Incident
from er.pushover_adapter import NullNotifier, PushoverNotifier
from er.ssm_secrets import NAMES, load_secrets, required_for
from er.twilio_adapter import NullTelephony

BASE_URL = "https://ring.example.invalid"
QUEUE = "https://sqs.invalid/queue"


def incident(**fields):
    return Incident(
        id="incident-aaa", sender="Robin", kind="kids", created_at=1, base_url=BASE_URL, **fields
    )


# ------------------------------------------------------------------ Pushover

class PushoverLeftovers(unittest.TestCase):
    def setUp(self):
        self.http = RecordedHttp(self, {"status": 1, "request": "req-1", "receipt": "rcpt123"})
        self.notifier = PushoverNotifier("app-token", "owner-key", {"Robin": "robin-key"})

    def sent(self):
        return self.http.requests[-1].fields

    def test_an_emergency_with_no_note_or_location_still_says_something(self):
        # The sender's Shortcut sends no note, and no location when it cannot
        # get one. Pushover's API requires message text all the same.
        self.assertEqual(self.notifier.send_emergency(incident()), "rcpt123")
        fields = self.sent()
        self.assertTrue(fields["message"].strip())
        self.assertEqual(fields["title"], "EMERGENCY from Robin: kids")
        self.assertEqual(fields["callback"], BASE_URL + "/pushover/ack/incident-aaa")
        for absent in ("url", "url_title", "sound"):
            self.assertNotIn(absent, fields)

    def test_the_map_link_needs_both_coordinates(self):
        self.notifier.send_emergency(incident(lat="47.610000", lon="-122.330000"))
        self.assertEqual(self.sent()["url"], "https://maps.apple.com/?ll=47.610000,-122.330000")
        self.assertTrue(self.sent()["url_title"])
        for half in (dict(lat="47.610000"), dict(lon="-122.330000"), dict(lat="", lon="")):
            self.notifier.send_emergency(incident(**half))
            self.assertNotIn("url", self.sent(), half)
            self.assertNotIn("url_title", self.sent(), half)

    def test_only_a_real_point_on_the_map_reaches_the_link(self):
        # From the front door: whatever a trigger carries as "lat" and "lon",
        # the link is either a clean pair of numbers or absent.
        def link_for(body):
            sim = support.make()
            sim.service.notifier = self.notifier
            status, _ = support.call("POST", "/trigger", dict(body, kind="kids"))
            self.assertEqual(status, 201, body)
            return self.sent().get("url")

        self.assertEqual(
            link_for({"lat": 47.61, "lon": "-122.33"}),
            "https://maps.apple.com/?ll=47.610000,-122.330000",
        )
        for junk in (
            {"lat": 47.61},                              # half a location
            {"lat": "91", "lon": "0"},                   # off the globe
            {"lat": "47.61&q=elsewhere", "lon": "0"},    # more than a number
            {"lat": "nan", "lon": "inf"},
            {"lat": True, "lon": 1},
            {"lat": None, "lon": None},
        ):
            self.assertIsNone(link_for(junk), junk)

    def test_a_drill_with_no_test_sound_falls_back_to_the_real_one(self):
        PushoverNotifier("app-token", "owner-key", sound="siren").send_emergency(incident(test=True))
        self.assertEqual(self.sent()["sound"], "siren")
        # With neither set, Pushover plays the user's own default.
        self.notifier.send_emergency(incident(test=True))
        self.assertNotIn("sound", self.sent())

    def test_a_reply_without_a_receipt_gives_none(self):
        self.http.reply = {"status": 1, "request": "req-1"}
        self.assertIsNone(self.notifier.send_emergency(incident()))

    def test_the_receipt_lookup_gets_a_shorter_timeout_than_a_send(self):
        # It runs on every tick, ahead of the redial it must not hold up.
        self.http.reply = {"status": 1, "acknowledged": 0}
        self.notifier.send_emergency(incident())
        self.notifier.emergency_acknowledged("rcpt123")
        self.assertEqual([sent.timeout for sent in self.http.requests], [8.0, 4.0])
        PushoverNotifier("app-token", "owner-key", timeout=2.0).emergency_acknowledged("rcpt123")
        self.assertEqual(self.http.requests[-1].timeout, 2.0)

    def test_the_receipt_cannot_change_the_address_of_the_lookup(self):
        self.http.reply = {"status": 1, "acknowledged": 1}
        self.notifier.emergency_acknowledged("r1/../x?y=1&token=other")
        self.assertEqual(
            self.http.requests[-1].url,
            "https://api.pushover.net/1/receipts/r1%2F..%2Fx%3Fy%3D1%26token%3Dother.json"
            "?token=app-token",
        )

    def test_a_plain_notice_carries_no_sound_or_link(self):
        self.notifier.send_notice("Assistant", "Call the school.")
        self.assertEqual(
            self.sent(),
            {"token": "app-token", "user": "owner-key", "title": "Assistant",
             "message": "Call the school.", "priority": "1"},
        )

    def test_every_failure_raises(self):
        # Core decides what a failure means (retry the push, keep calling,
        # release the break-glass slot); it can only do so if it hears of it.
        calls = (
            lambda: self.notifier.send_emergency(incident()),
            lambda: self.notifier.cancel_emergency("rcpt123"),
            lambda: self.notifier.emergency_acknowledged("rcpt123"),
            lambda: self.notifier.send_notice("Assistant", "Call the school."),
            lambda: self.notifier.notify_sender("Robin", "Acknowledged."),
        )
        for n, call in enumerate(calls):
            self.http.reply = http_error(500)
            with self.assertRaises(urllib.error.HTTPError, msg=n):
                call()
            self.http.reply = socket.timeout("timed out")
            with self.assertRaises(socket.timeout, msg=n):
                call()


# ------------------------------------------------ one arm off: the stand-ins

class NullAdapters(unittest.TestCase):
    def test_the_stand_in_notifier_sends_nothing_and_confirms_nothing(self):
        notifier = NullNotifier()
        self.assertIsNone(notifier.send_emergency(incident()))
        self.assertIsNone(notifier.cancel_emergency("rcpt123"))
        # Never "acknowledged": that would stop the calls.
        self.assertIs(notifier.emergency_acknowledged("rcpt123"), False)
        self.assertIsNone(notifier.send_notice("Assistant", "Call the school.", sound="bike",
                                               url="https://example.invalid/vm"))
        self.assertIsNone(notifier.notify_sender("Robin", "Acknowledged."))

    def test_the_stand_in_telephony_refuses_to_dial(self):
        telephony = NullTelephony()
        # Loud: a dial with the call arm off is a bug, and a made-up call id
        # would be counted as a call placed.
        with self.assertRaises(RuntimeError):
            telephony.place_call(PRIMARY, "incident-aaa", BASE_URL, 25)
        self.assertIsNone(telephony.hangup("CA1"))

    def test_push_only_never_reaches_the_stand_in_telephony(self):
        sim = Simulation(Config(call_enabled=False), lambda n, to: NO_ANSWER)
        sim.service.telephony = NullTelephony()
        started = sim.trigger()
        sim.run()
        ended = sim.store.get(started.id)
        # A dial would have raised and been counted as a failed call.
        self.assertEqual((ended.status, ended.attempts, ended.calls_failed), (EXPIRED, 0, 0))
        self.assertEqual(len(sim.notifier.emergencies), 1)

    def test_push_only_can_be_cancelled_with_nothing_to_hang_up(self):
        sim = Simulation(Config(call_enabled=False), lambda n, to: NO_ANSWER)
        sim.service.telephony = NullTelephony()
        started = sim.trigger()
        self.assertTrue(sim.service.cancel(started.id, "Robin"))
        self.assertEqual(sim.store.get(started.id).status, CANCELLED)
        self.assertEqual(sim.notifier.cancelled, ["rcpt-" + started.id])

    def call_only(self, behaviour=lambda n, to: NO_ANSWER):
        sim = Simulation(
            Config(primary_number=PRIMARY, push_enabled=False, primary_attempts=2), behaviour
        )
        # sim.notifier stays the unused fake: the simulation reads its lists.
        sim.service.notifier = NullNotifier()
        return sim

    def test_call_only_runs_to_the_end_on_the_stand_in_notifier(self):
        sim = self.call_only()
        started = sim.trigger()
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertIsNone(sim.store.get(started.id).pushover_receipt)
        # A callback, or a queued push left over from before the arm was
        # switched off, finds nothing to act on.
        self.assertFalse(sim.service.acknowledge_from_push(started.id, "rcpt123"))
        sim.service.deliver_push(started.id)
        self.assertIsNone(sim.store.get(started.id).pushover_receipt)
        sim.run()
        ended = sim.store.get(started.id)
        self.assertEqual((ended.status, ended.attempts, ended.calls_failed), (EXPIRED, 2, 0))

    def test_call_only_is_acknowledged_by_keypress(self):
        sim = self.call_only(lambda n, to: ACK if n == 2 else NO_ANSWER)
        started = sim.trigger()
        sim.run()
        ended = sim.store.get(started.id)
        self.assertEqual((ended.status, ended.acked_via, ended.attempts), (ACKNOWLEDGED, "call", 2))

    def test_break_glass_is_refused_outright_without_the_push_arm(self):
        # Otherwise the stand-in would swallow the alert and report it delivered.
        sim = self.call_only()
        with self.assertRaises(RuntimeError):
            sim.service.break_glass("Assistant", "Call the school.")


# ----------------------------------------------------------------------- SQS

class StubSqs:
    def __init__(self):
        self.sent = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)


class SqsMessages(unittest.TestCase):
    def setUp(self):
        self.sqs = StubSqs()
        self.scheduler = SqsScheduler(QUEUE, client=self.sqs)

    def test_a_message_is_a_queue_a_delay_and_a_json_body(self):
        self.scheduler.schedule_tick("incident-aaa", 3, 60)
        self.scheduler.schedule_push("incident-aaa", 30)
        tick, push = self.sqs.sent
        self.assertEqual(
            tick, {"QueueUrl": QUEUE, "DelaySeconds": 60, "MessageBody": tick["MessageBody"]}
        )
        self.assertEqual(json.loads(tick["MessageBody"]), {"incident_id": "incident-aaa", "seq": 3})
        self.assertEqual(
            push, {"QueueUrl": QUEUE, "DelaySeconds": 30, "MessageBody": push["MessageBody"]}
        )
        self.assertEqual(
            json.loads(push["MessageBody"]), {"incident_id": "incident-aaa", "action": "push"}
        )

    def test_the_delay_is_always_a_whole_number_sqs_accepts(self):
        # SQS refuses a delay outside 0 to 900 seconds, or one that is not an
        # integer, and a refused tick is a redial that never happens.
        asked = (0, 1, MAX_DELAY - 1, MAX_DELAY, MAX_DELAY + 1, 10 ** 6, -1, 12.7, Decimal(30))
        for delay in asked:
            self.scheduler.schedule_tick("incident-aaa", 0, delay)
            self.scheduler.schedule_push("incident-aaa", delay)
        delays = [message["DelaySeconds"] for message in self.sqs.sent]
        self.assertEqual(delays[::2], [0, 1, 899, 900, 900, 900, 0, 12, 30])
        self.assertEqual(delays[1::2], delays[::2])
        for delay in delays:
            self.assertIs(type(delay), int)

# ------------------------------------------------------------------- secrets

class StubSsm:
    def __init__(self, parameters):
        self.parameters = parameters
        self.asked = []

    def get_parameters(self, Names, WithDecryption=False):
        self.asked.append(list(Names))
        return {
            "Parameters": [
                {"Name": name, "Type": "SecureString", "Value": self.parameters[name]}
                for name in Names if name in self.parameters
            ],
            "InvalidParameters": [name for name in Names if name not in self.parameters],
        }


def parameters(prefix="/emergency-ring", **changed):
    values = {
        "trigger-tokens": '{"Robin": "robin-secret"}',
        "twilio-auth-token": "twilio-secret",
        "pushover-app-token": "app-secret",
        "pushover-user-key": "user-secret",
        "break-glass-tokens": '{"Assistant": "assistant-secret"}',
    }
    values.update({name.replace("_", "-"): value for name, value in changed.items()})
    return {"%s/%s" % (prefix, name): value for name, value in values.items() if value is not None}


BOTH_ARMS = required_for(True, True)


class SecretsLeftovers(unittest.TestCase):
    def test_everything_fits_in_the_one_request(self):
        # GetParameters takes at most ten names. An eleventh would need a
        # second call, and the loader makes only one.
        self.assertLessEqual(len(NAMES), 10)
        ssm = StubSsm(parameters())
        load_secrets("/emergency-ring", BOTH_ARMS, client=ssm)
        self.assertEqual(ssm.asked, [["/emergency-ring/" + name for name in NAMES]])

    def test_the_agent_tokens_are_returned_when_present_and_never_required(self):
        found = load_secrets("/emergency-ring", BOTH_ARMS, client=StubSsm(parameters()))
        self.assertEqual(found["break-glass-tokens"], '{"Assistant": "assistant-secret"}')
        for calls in (True, False):
            for push in (True, False):
                self.assertNotIn("break-glass-tokens", required_for(calls, push))
                self.assertIn("trigger-tokens", required_for(calls, push))
        without = StubSsm(parameters(break_glass_tokens=None))
        self.assertNotIn("break-glass-tokens", load_secrets("/emergency-ring", BOTH_ARMS, client=without))

    def test_a_prefix_with_several_levels_gives_the_same_short_names(self):
        ssm = StubSsm(parameters(prefix="/home/ring"))
        found = load_secrets("/home/ring/", BOTH_ARMS, client=ssm)
        self.assertEqual(sorted(found), sorted(NAMES))
        self.assertEqual(found["twilio-auth-token"], "twilio-secret")

    def test_every_missing_secret_is_named_and_no_value_is_shown(self):
        ssm = StubSsm(parameters(twilio_auth_token=None, pushover_user_key=""))
        with self.assertRaises(RuntimeError) as raised:
            load_secrets("/emergency-ring", BOTH_ARMS, client=ssm)
        message = str(raised.exception)
        # An empty value is no better than none.
        self.assertIn("/emergency-ring/twilio-auth-token", message)
        self.assertIn("/emergency-ring/pushover-user-key", message)
        self.assertNotIn("trigger-tokens", message)
        self.assertNotIn("secret", message)

    def test_a_reply_with_no_parameters_at_all_reads_as_missing(self):
        class Empty:
            def get_parameters(self, Names, WithDecryption=False):
                return {"InvalidParameters": list(Names)}

        with self.assertRaises(RuntimeError) as raised:
            load_secrets("/emergency-ring", required_for(False, True), client=Empty())
        self.assertIn("/emergency-ring/trigger-tokens", str(raised.exception))

    def test_a_prefix_that_is_not_a_path_is_refused_before_anything_is_asked(self):
        for prefix in ("", "/", "emergency-ring", "emergency-ring/"):
            ssm = StubSsm(parameters())
            with self.assertRaises(RuntimeError, msg=prefix):
                load_secrets(prefix, BOTH_ARMS, client=ssm)
            self.assertEqual(ssm.asked, [], prefix)


# ------------------------------------------------ clients nobody passed in

def stand_in_boto3(made):
    """A module that answers boto3.client and boto3.resource from `made`
    (service name -> object) and notes what it was asked for."""
    module = types.ModuleType("boto3")
    module.asked = []

    def build(kind):
        def make(service, **kwargs):
            module.asked.append((kind, service, kwargs))
            return made[service]
        return make

    module.client = build("client")
    module.resource = build("resource")
    return module


class DefaultClients(unittest.TestCase):
    """On Lambda the adapters build their own clients. Each must ask for the
    right service and hand over the fail-fast settings: boto3's defaults wait
    far longer than the function is allowed to run."""

    def setUp(self):
        self.sqs, self.ssm, self.dynamodb = StubSqs(), StubSsm(parameters()), StubResource()
        self.boto3 = stand_in_boto3({"sqs": self.sqs, "ssm": self.ssm, "dynamodb": self.dynamodb})
        self.settings = object()
        for patcher in (
            mock.patch.dict(sys.modules, {"boto3": self.boto3}),
            mock.patch.object(aws_adapters, "client_config", lambda: self.settings),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_scheduler_builds_an_sqs_client(self):
        SqsScheduler(QUEUE).schedule_tick("incident-aaa", 0, 30)
        self.assertEqual(self.boto3.asked, [("client", "sqs", {"config": self.settings})])
        self.assertEqual(len(self.sqs.sent), 1)

    def test_the_store_builds_a_dynamodb_resource(self):
        store = DynamoStore(TABLE, Config(primary_number=PRIMARY))
        self.assertEqual(self.boto3.asked, [("resource", "dynamodb", {"config": self.settings})])
        store.set_pause(1_000_600, "Robin")
        self.assertEqual(store.pause_state(), (1_000_600, "Robin"))

    def test_the_secrets_loader_builds_an_ssm_client(self):
        found = load_secrets("/emergency-ring", BOTH_ARMS)
        self.assertEqual(self.boto3.asked, [("client", "ssm", {"config": self.settings})])
        self.assertEqual(found["twilio-auth-token"], "twilio-secret")

    def test_a_client_that_is_passed_in_is_used_as_it_is(self):
        SqsScheduler(QUEUE, client=self.sqs)
        DynamoStore(TABLE, Config(primary_number=PRIMARY), resource=self.dynamodb)
        load_secrets("/emergency-ring", BOTH_ARMS, client=self.ssm)
        self.assertEqual(self.boto3.asked, [])


class ClientSettings(unittest.TestCase):
    def check(self, connect_timeout, read_timeout, retries):
        self.assertGreater(connect_timeout, 0)
        self.assertGreater(read_timeout, 0)
        # "standard" backs off between tries and caps them; "legacy", the
        # default, does not count the first try the same way.
        self.assertEqual(retries["mode"], "standard")
        attempts = retries["total_max_attempts"]
        self.assertGreaterEqual(attempts, 2)  # one retry rides out a blip
        # One AWS call, every retry included, has to give up while the
        # function is still alive to report the failure.
        self.assertLess(attempts * (connect_timeout + read_timeout), function_timeout())

    def test_one_slow_aws_call_gives_up_before_the_function_is_killed(self):
        # botocore stands in as a recorder, so this runs without it installed.
        seen = {}
        config_module = types.ModuleType("botocore.config")
        config_module.Config = lambda **kwargs: seen.update(kwargs) or "built"
        modules = {"botocore": types.ModuleType("botocore"), "botocore.config": config_module}
        with mock.patch.dict(sys.modules, modules):
            self.assertEqual(client_config(), "built")
        self.assertEqual(sorted(seen), ["connect_timeout", "read_timeout", "retries"])
        self.check(**seen)

    def test_botocore_accepts_the_settings(self):
        try:
            from botocore.config import Config as BotoConfig
        except ImportError:
            self.skipTest("botocore is not installed here; the Lambda runtime provides it")
        built = client_config()
        self.assertIsInstance(built, BotoConfig)
        self.check(built.connect_timeout, built.read_timeout, built.retries)


class Imports(unittest.TestCase):
    def test_importing_the_code_does_not_import_boto3(self):
        # boto3 comes from the Lambda runtime and is slow to import. It is
        # loaded where a client is built, so the code imports without it:
        # here, and in the tests.
        source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
        code = (
            "import sys; sys.path.insert(0, %r); "
            "import er.handler, er.aws_adapters, er.ssm_secrets, er.core, er.fakes, "
            "er.pushover_adapter, er.twilio_adapter; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('boto3', 'botocore')))"
        ) % source
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        # With this set the handler starts up as it does on Lambda, clients and all.
        env.pop("AWS_LAMBDA_FUNCTION_NAME", None)
        done = subprocess.run(
            [sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
        )
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "[]"), done.stderr)


if __name__ == "__main__":
    unittest.main()
