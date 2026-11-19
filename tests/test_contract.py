"""Agreements between parts that are written down in different places, which
nothing else checks: the environment variables the template sets against the
ones the code reads, the URLs handed to Twilio and Pushover against the routes
that answer them, and the queue messages the scheduler writes against the
handler that reads them.

template.yaml is read as text. The project has no YAML library and these
checks do not need one; if the template's layout changes, the small readers at
the top of this file are the place to adjust.
"""
import dataclasses
import importlib
import json
import os
import re
import unittest
import urllib.parse
import xml.dom.minidom
from unittest import mock

from support import (
    ASSISTANT, BACKUP, BASE, PRIMARY, ROBIN, TWILIO_TOKEN, call, event, make, request,
)

from er import handler
from er.aws_adapters import SqsScheduler
from er.config import Config
from er.core import SELF_TEST_SENDER
from er.models import ACKNOWLEDGED, ACTIVE, CANCELLED
from er.pushover_adapter import PushoverNotifier
from er.twilio_adapter import TwilioTelephony, compute_signature

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
with open(os.path.join(ROOT, "template.yaml"), encoding="utf-8") as _template:
    TEMPLATE = _template.read()


# ------------------------------------------------------ reading the template

def _indent(line):
    return len(line) - len(line.lstrip(" "))


def block(heading):
    """The lines under the first line matching `heading`, up to where the
    indentation comes back to that line's level."""
    lines = TEMPLATE.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(heading, line))
    body = []
    for line in lines[start + 1:]:
        if line.strip() and _indent(line) <= _indent(lines[start]):
            break
        body.append(line)
    return body


def function_variables():
    """{environment variable: what the template sets it to} for the function."""
    found = {}
    for line in block(r"\s+Variables:\s*$"):
        match = re.match(r"\s+([A-Z][A-Z0-9_]*):\s*(\S.*)$", line)
        if match:
            found[match.group(1)] = match.group(2).strip()
    return found


def parameter(name):
    """{property: text} for one template parameter, quotes removed."""
    found = {}
    for line in block(r"  %s:\s*$" % re.escape(name)):
        match = re.match(r"    (\w+):\s*(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2).strip().strip("'\"")
    return found


def listed(text):
    """The items of a one-line YAML list such as ['true', 'false']."""
    return [item.strip().strip("'\"") for item in text.strip("[]").split(",")]


# What the template would pass for each setting if it were given a value that
# differs from the default, the Config field it lands in, and the value
# expected there. A variable missing from here fails
# test_every_variable_is_accounted_for.
SETTINGS = {
    "OWNER_NAME": ("Alex", "owner_name", "Alex"),
    "CALL_ENABLED": ("false", "call_enabled", False),
    "PUSH_ENABLED": ("false", "push_enabled", False),
    "FIRST_ARM": ("call", "first_arm", "call"),
    "ARM_OFFSET_SECONDS": ("45", "arm_offset_seconds", 45),
    "CYCLE_SECONDS": ("120", "cycle_seconds", 120),
    "PRIMARY_ATTEMPTS": ("4", "primary_attempts", 4),
    "BACKUP_ATTEMPTS": ("2", "backup_attempts", 2),
    "RING_TIMEOUT": ("20", "ring_timeout", 20),
    "PUSH_SOUND": ("siren", "push_sound", "siren"),
    "TEST_PUSH_SOUND": ("vibrate", "test_push_sound", "vibrate"),
    "BREAK_GLASS_MINUTES": ("20", "break_glass_interval_seconds", 1200),
    "BREAK_GLASS_SOUND": ("bike", "break_glass_sound", "bike"),
    "BREAK_GLASS_DAILY_CAP": ("3", "break_glass_daily_cap", 3),
    "PRIMARY_NUMBER": ("+15550000003", "primary_number", "+15550000003"),
    "BACKUP_NUMBER": (BACKUP, "backup_number", BACKUP),
    "SENDER_PUSHOVER_KEYS": ('{"Robin": "robin-user-key"}', "sender_pushover_keys",
                             {"Robin": "robin-user-key"}),
}
# Variables that do not become settings: they say where the table, the queue,
# the secrets and the Twilio account are, and what the public URL is.
WIRING = {"TABLE_NAME", "TICK_QUEUE_URL", "SSM_PREFIX", "TWILIO_ACCOUNT_SID",
          "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL"}
# Set by Lambda itself, not by the template.
FROM_THE_RUNTIME = {"AWS_LAMBDA_FUNCTION_NAME"}

ENV = {
    "SSM_PREFIX": "/emergency-ring-example",
    "TABLE_NAME": "incidents-table",
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


class Asked(dict):
    """An environment that remembers every name it was asked for."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.names = set()

    def __getitem__(self, name):
        self.names.add(name)
        return super().__getitem__(name)

    def get(self, name, default=None):
        self.names.add(name)
        return super().get(name, default)

    def __contains__(self, name):
        self.names.add(name)
        return super().__contains__(name)


def build(env):
    """Build the service as a cold start does, in the given environment, with
    SSM, DynamoDB and SQS replaced so that nothing is reached. Returns the
    service and the three stand-ins."""
    with mock.patch.object(os, "environ", env), \
            mock.patch("er.ssm_secrets.load_secrets", return_value=SECRETS) as secrets, \
            mock.patch("er.aws_adapters.DynamoStore") as store, \
            mock.patch("er.aws_adapters.SqsScheduler") as scheduler:
        return handler.build_service(), secrets, store, scheduler


class TemplateAndEnvironment(unittest.TestCase):
    def test_the_template_sets_exactly_the_variables_the_code_asks_for(self):
        # A variable the code reads and the template never sets is a setting
        # that silently stays at its default; one the template sets and the
        # code never reads is a parameter that does nothing.
        env = Asked(ENV)
        build(env)
        make()
        env.update(PUBLIC_BASE_URL=BASE)   # the timer's self-test will not run without it
        with mock.patch.object(os, "environ", env):
            request("GET", "/")
            handler.lambda_handler({"action": "selftest"})
        self.assertEqual(env.names - FROM_THE_RUNTIME, set(function_variables()))

    def test_no_other_variable_name_is_written_in_the_code_that_reads_them(self):
        # The same agreement from the source text, for a read on some path
        # the test above does not walk.
        written = set()
        for name in ("config.py", "handler.py"):
            with open(os.path.join(ROOT, "src", "er", name), encoding="utf-8") as source:
                written |= set(re.findall(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"', source.read()))
        self.assertEqual(written - FROM_THE_RUNTIME, set(function_variables()))

    def test_every_variable_is_accounted_for(self):
        self.assertEqual(set(SETTINGS) | WIRING, set(function_variables()))

    def test_every_variable_refers_to_something_the_template_declares(self):
        declared = set(re.findall(r"^  (\w+):\s*$", TEMPLATE, re.M))
        for variable, value in function_variables().items():
            match = re.match(r"!Ref (\w+)$", value)
            self.assertTrue(match, "%s is set to %r" % (variable, value))
            self.assertIn(match.group(1), declared, variable)

    def test_each_setting_reaches_its_own_field_and_no_other(self):
        defaults = Config(primary_number=PRIMARY)
        for variable, (text, field, expected) in SETTINGS.items():
            with self.subTest(variable=variable):
                config = Config.from_env(env={"PRIMARY_NUMBER": PRIMARY, variable: text})
                self.assertEqual(config, dataclasses.replace(defaults, **{field: expected}))
                self.assertNotEqual(getattr(defaults, field), expected,
                                    "the example must differ from the default to prove anything")

    def test_a_variable_that_is_unset_or_empty_leaves_the_default(self):
        defaults = Config(primary_number=PRIMARY)
        self.assertEqual(Config.from_env(env={"PRIMARY_NUMBER": PRIMARY}), defaults)
        # The template passes '' for every text parameter left at its default.
        empty = dict({variable: "" for variable in SETTINGS}, PRIMARY_NUMBER=PRIMARY)
        self.assertEqual(Config.from_env(env=empty), defaults)

    def test_zero_is_a_value_not_a_missing_one(self):
        # 0 is how the break-glass interval and the daily cap are switched off.
        config = Config.from_env(env={
            "PRIMARY_NUMBER": PRIMARY, "BREAK_GLASS_MINUTES": "0", "BREAK_GLASS_DAILY_CAP": "0",
            "BACKUP_ATTEMPTS": "0", "ARM_OFFSET_SECONDS": "0",
        })
        self.assertEqual(
            (config.break_glass_interval_seconds, config.break_glass_daily_cap,
             config.backup_attempts, config.arm_offset_seconds),
            (0, 0, 0, 0),
        )

    def test_every_default_and_limit_the_template_allows_is_one_the_code_accepts(self):
        # CloudFormation checks a parameter against these before deploying.
        # A value it lets through and Config then refuses would deploy
        # cleanly and fail on the first request.
        checked = set()
        for variable, value in function_variables().items():
            if variable not in SETTINGS:
                continue
            properties = parameter(value.split()[-1])
            allowed = [properties[key] for key in ("MinValue", "MaxValue") if key in properties]
            allowed += listed(properties.get("AllowedValues", "[]"))
            for text in filter(None, allowed):
                with self.subTest(variable=variable, value=text):
                    Config.from_env(env={"PRIMARY_NUMBER": PRIMARY, variable: text})
                    checked.add(variable)
        # The readers above did find the limits; a silent miss proves nothing.
        self.assertLessEqual({"CYCLE_SECONDS", "ARM_OFFSET_SECONDS", "FIRST_ARM", "CALL_ENABLED"},
                             checked)

    def test_a_negative_break_glass_limit_is_refused_by_the_code_as_by_the_template(self):
        # Below zero the arithmetic goes wrong quietly: a negative cap would
        # refuse every alert, and a negative interval would allow them all.
        for variable in ("BREAK_GLASS_MINUTES", "BREAK_GLASS_DAILY_CAP"):
            name = function_variables()[variable].split()[-1]
            self.assertEqual(parameter(name)["MinValue"], "0", name)
            with self.assertRaises(ValueError, msg=variable):
                Config.from_env(env={"PRIMARY_NUMBER": PRIMARY, variable: "-1"})

    def test_the_wiring_variables_reach_the_clients_that_need_them(self):
        env = dict(ENV, **{variable: text for variable, (text, _, _) in SETTINGS.items()
                           if variable not in ("CALL_ENABLED", "PUSH_ENABLED")})
        service, secrets, store, scheduler = build(env)
        secrets.assert_called_once_with("/emergency-ring-example", required=mock.ANY)
        store.assert_called_once_with("incidents-table", service.cfg)
        scheduler.assert_called_once_with("https://sqs.example.invalid/ticks")
        self.assertEqual((service.telephony.account_sid, service.telephony.from_number),
                         ("ACexample", "+15550000009"))
        # The settings the push adapter needs are handed to it when it is built.
        notifier, config = service.notifier, service.cfg
        self.assertEqual((notifier.sound, notifier.test_sound), ("siren", "vibrate"))
        self.assertEqual(notifier.sender_keys, {"Robin": "robin-user-key"})
        self.assertEqual((notifier.retry, notifier.expire),
                         (config.cycle_seconds, config.pushover_expire))

    def test_the_public_url_variable_is_the_base_of_every_callback(self):
        sim = make()
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://ring.example.invalid"}):
            _, body = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(sim.store.get(body["id"]).base_url, "https://ring.example.invalid")


class TemplateAndHandler(unittest.TestCase):
    def test_the_handler_the_template_names_exists(self):
        code_uri = re.search(r"^\s+CodeUri:\s*(\S+)", TEMPLATE, re.M).group(1)
        dotted = re.search(r"^\s+Handler:\s*(\S+)", TEMPLATE, re.M).group(1)
        module_name, function_name = dotted.rsplit(".", 1)
        path = os.path.join(ROOT, code_uri, *module_name.split(".")) + ".py"
        self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(callable(getattr(importlib.import_module(module_name), function_name)))

    def test_the_timers_event_is_one_the_handler_takes_for_a_self_test(self):
        # Any other shape would be read as an HTTP request and answered with
        # a 404 that nobody sees: the monthly test would quietly never run.
        payload = json.loads(re.search(r"^\s+Input:\s*'(.*)'\s*$", TEMPLATE, re.M).group(1))
        sim = make()
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE}):
            result = handler.lambda_handler(payload)
        incident = sim.store.get(result["incident"])
        self.assertEqual((incident.sender, incident.test), (SELF_TEST_SENDER, True))

    def test_the_queue_hides_a_tick_for_at_least_as_long_as_the_function_may_run(self):
        # Otherwise SQS hands the same tick to a second copy of the function
        # while the first is still working on it.
        visibility = int(re.search(r"^\s+VisibilityTimeout:\s*(\d+)", TEMPLATE, re.M).group(1))
        timeout = int(re.search(r"^\s+Timeout:\s*(\d+)", TEMPLATE, re.M).group(1))
        self.assertGreaterEqual(visibility, timeout)


# ------------------------------------------------- callbacks and the routes

class Reply:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class Vendors:
    """Twilio's and Pushover's HTTP APIs, as far as the adapters use them.
    Keeps every request, so a test can see what the vendors were told."""

    def __init__(self):
        self.requests = []

    def urlopen(self, request, timeout=None):
        url = request.full_url
        fields = urllib.parse.parse_qs(request.data.decode()) if request.data else {}
        self.requests.append((request.get_method(), url, {k: v[0] for k, v in fields.items()}))
        if url.startswith("https://api.twilio.com/"):
            return Reply({"sid": "CAexample%04d" % len(self.requests)})
        if url.startswith("https://api.pushover.net/1/receipts/"):
            return Reply({"status": 1, "acknowledged": 1})
        if url == "https://api.pushover.net/1/messages.json":
            return Reply({"status": 1, "receipt": "receipt" + "0" * 23})
        raise AssertionError("unexpected request to " + url)

    def sent_to(self, url_part):
        """The fields of the one request whose URL contains url_part."""
        matching = [fields for _, url, fields in self.requests if url_part in url]
        assert len(matching) == 1, (url_part, self.requests)
        return matching[0]


class CallbackUrls(unittest.TestCase):
    """The real adapters tell Twilio and Pushover where to call back. Each of
    those URLs is taken exactly as the vendor received it and posted to the
    handler the way that vendor would, so a URL that matches no route, or that
    the route's own check would turn away, fails here and not on a live call."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PUBLIC_BASE_URL", None)
        self.vendors = Vendors()
        patcher = mock.patch("urllib.request.urlopen", self.vendors.urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.sim = make(arm_offset_seconds=0)
        self.sim.service.telephony = TwilioTelephony("ACexample", TWILIO_TOKEN, "+15550000009")
        self.sim.service.notifier = PushoverNotifier("app-token", "owner-key")
        status, body = call("POST", "/trigger", {"kind": "kids"})
        self.assertEqual(status, 201)
        self.id = body["id"]
        self.call = self.vendors.sent_to("/Calls.json")
        self.push = self.vendors.sent_to("/messages.json")

    def incident(self):
        return self.sim.store.get(self.id)

    def route(self, method, url):
        """Who may use the one route that answers this URL."""
        self.assertTrue(url.startswith(BASE + "/"), url)
        path = urllib.parse.urlsplit(url).path
        matching = [who for verb, pattern, who, _ in handler.ROUTES
                    if verb == method and pattern.match(path)]
        self.assertEqual(len(matching), 1, "%s %s must match one route" % (method, url))
        return matching[0]

    def as_twilio(self, url, fields):
        """Twilio posting to a URL it was given, signed for exactly that URL."""
        parts = urllib.parse.urlsplit(url)
        made = event("POST", parts.path, raw=urllib.parse.urlencode(fields),
                     headers={"X-Twilio-Signature": compute_signature(TWILIO_TOKEN, url, fields)})
        made["rawQueryString"] = parts.query
        made["requestContext"]["domainName"] = parts.netloc
        return handler.lambda_handler(made)

    def keypress_urls(self):
        """Where the spoken message tells Twilio to send a keypress."""
        voice = self.as_twilio(self.call["Url"], {"CallSid": self.incident().last_call_sid})
        self.assertEqual((voice["statusCode"], voice["headers"]["content-type"]), (200, "text/xml"))
        gathers = xml.dom.minidom.parseString(voice["body"]).getElementsByTagName("Gather")
        return [(gather.getAttribute("method"), gather.getAttribute("action")) for gather in gathers]

    def test_twilio_is_given_the_voice_route_and_can_use_it(self):
        self.assertEqual(self.call["Method"], "POST")
        # None: the route checks Twilio's signature itself, which is the only
        # kind of check Twilio can pass.
        self.assertIsNone(self.route("POST", self.call["Url"]))
        self.assertIn(self.id, self.call["Url"])
        self.assertEqual(self.call["To"], PRIMARY)
        voice = self.as_twilio(self.call["Url"], {"CallSid": self.incident().last_call_sid})
        self.assertEqual(voice["statusCode"], 200)
        self.assertIn("Press 1 to acknowledge", voice["body"])

    def test_twilio_is_given_the_status_route_and_can_use_it(self):
        self.assertEqual(self.call["StatusCallbackMethod"], "POST")
        self.assertIsNone(self.route("POST", self.call["StatusCallback"]))
        self.assertIsNotNone(self.incident().call_in_flight_since)
        sid = self.incident().last_call_sid
        ended = self.as_twilio(self.call["StatusCallback"], {"CallSid": sid, "CallStatus": "no-answer"})
        self.assertEqual(ended["statusCode"], 200)
        self.assertIsNone(self.incident().call_in_flight_since)

    def test_the_message_is_said_three_times_and_each_keypress_goes_to_the_ack_route(self):
        urls = self.keypress_urls()
        self.assertEqual(len(urls), 3)
        self.assertEqual(len(set(urls)), 1)
        method, url = urls[0]
        self.assertEqual(method, "POST")
        self.assertIsNone(self.route("POST", url))
        pressed = self.as_twilio(url, {"Digits": "1", "CallSid": self.incident().last_call_sid})
        self.assertEqual(pressed["statusCode"], 200)
        self.assertEqual((self.incident().status, self.incident().acked_via), (ACKNOWLEDGED, "call"))

    def test_pushover_is_given_the_ack_route_and_can_use_it(self):
        self.assertIsNone(self.route("POST", self.push["callback"]))
        receipt = self.incident().pushover_receipt
        path = urllib.parse.urlsplit(self.push["callback"]).path
        # Pushover's callback carries no credentials of any kind.
        status, body = call("POST", path, token=None,
                            raw=urllib.parse.urlencode({"receipt": receipt, "acknowledged": "1"}))
        self.assertEqual((status, body), (200, {"acknowledged": True}))
        self.assertEqual((self.incident().status, self.incident().acked_via),
                         (ACKNOWLEDGED, "pushover"))
        # It counted only because Pushover's own API confirmed it.
        asked = [url for method, url, _ in self.vendors.requests if method == "GET"]
        self.assertEqual(
            asked, ["https://api.pushover.net/1/receipts/%s.json?token=app-token" % receipt])

    def test_closing_takes_back_the_very_call_and_push_the_vendors_reported(self):
        sid, receipt = self.incident().last_call_sid, self.incident().pushover_receipt
        self.assertTrue(sid.startswith("CAexample") and receipt.startswith("receipt"))
        self.assertEqual(call("POST", "/incidents/%s/cancel" % self.id)[0], 200)
        self.assertEqual(self.incident().status, CANCELLED)
        self.assertEqual(self.vendors.sent_to("/Calls/%s.json" % sid), {"Status": "completed"})
        self.assertEqual(self.vendors.sent_to("/receipts/%s/cancel.json" % receipt),
                         {"token": "app-token"})


def example_path(pattern, incident_id):
    """A path this route's pattern matches, for the given incident."""
    path = re.sub(r"\(.*?\)", incident_id, pattern.pattern.strip("^$"))
    assert pattern.match(path), (pattern.pattern, path)
    return path


class Routes(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PUBLIC_BASE_URL", None)

    def test_no_route_does_anything_for_a_caller_with_no_credentials(self):
        # Walks handler.ROUTES itself, so a route added later is covered
        # without anyone remembering to list it in a test.
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        form = "Digits=1&CallSid=CA0001&CallStatus=completed&acknowledged=1&receipt=guess"
        for method, pattern, who, _ in handler.ROUTES:
            path = example_path(pattern, incident.id)
            with self.subTest(route="%s %s" % (method, path)):
                if who is not None:
                    self.assertIn(who, (handler.SENDER, handler.AGENT, handler.ANYONE))
                    self.assertEqual(call(method, path, {"kind": "kids"}, token=None)[0], 401)
                elif path.startswith("/twilio/"):
                    with self.assertLogs("er", level="WARNING"):
                        self.assertEqual(call(method, path, token=None, raw=form)[0], 403)
                else:
                    # Only Pushover's callback is open, and it does nothing
                    # until Pushover itself confirms the receipt.
                    self.assertTrue(path.startswith("/pushover/ack/"), path)
                    self.assertEqual(call(method, path, token=None, raw=form),
                                     (200, {"acknowledged": False}))
        after = sim.store.get(incident.id)
        self.assertEqual((after.status, after.call_in_flight_since), (ACTIVE, sim.clock.now()))
        self.assertEqual(len(sim.store.items), 1)
        self.assertEqual(sim.service.paused_seconds(), 0)
        self.assertEqual((sim.notifier.notices, sim.notifier.cancelled, sim.telephony.hung_up),
                         ([], [], []))

    def test_an_agents_token_opens_no_route_that_starts_or_cancels_an_emergency(self):
        sim = make(arm_offset_seconds=0)
        incident = sim.trigger()
        for method, pattern, who, route in handler.ROUTES:
            if who != handler.SENDER:
                continue
            path = example_path(pattern, incident.id)
            self.assertEqual(call(method, path, {"kind": "kids"}, token=ASSISTANT)[0], 401, path)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)
        self.assertEqual(len(sim.store.items), 1)


# ----------------------------------------------------------- queue messages

class RecordingQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, **message):
        self.messages.append(message)


class QueueMessages(unittest.TestCase):
    """What the real scheduler puts on the queue is what the handler takes
    off it. Each side has its own tests against a literal message; this one
    passes the scheduler's own output to the handler."""

    def setUp(self):
        self.queue = RecordingQueue()
        self.sim = make(first_arm="call")
        self.sim.service.scheduler = SqsScheduler("https://sqs.example.invalid/ticks",
                                                  client=self.queue)

    def deliver(self, message):
        return handler.lambda_handler({"Records": [{"body": message["MessageBody"]}]})

    def test_a_tick_and_a_delayed_push_survive_the_round_trip(self):
        sim, cfg = self.sim, self.sim.cfg
        incident = sim.trigger()         # call first; the push and the next tick are queued
        tick, push = self.queue.messages
        self.assertEqual((tick["DelaySeconds"], push["DelaySeconds"]),
                         (cfg.cycle_seconds, cfg.arm_offset_seconds))

        sim.clock.t += push["DelaySeconds"]
        self.assertEqual(self.deliver(push), {"ok": True})
        self.assertEqual(sim.notifier.emergencies, ["rcpt-" + incident.id])

        sim.service.call_status(incident.id, "CA0001", "no-answer")
        sim.clock.t += tick["DelaySeconds"] - push["DelaySeconds"]
        self.assertEqual(self.deliver(tick), {"ok": True})
        self.assertEqual(len(sim.telephony.calls), 2)

        # The tick queued the one after it, and that one works too.
        following = self.queue.messages[-1]
        self.assertEqual(len(self.queue.messages), 3)
        sim.service.call_status(incident.id, "CA0002", "no-answer")
        sim.clock.t += following["DelaySeconds"]
        self.deliver(following)
        self.assertEqual(len(sim.telephony.calls), 3)
        # A message delivered a second time is recognised as used.
        self.deliver(tick)
        self.deliver(push)
        self.assertEqual((len(sim.telephony.calls), len(sim.notifier.emergencies)), (3, 1))


if __name__ == "__main__":
    unittest.main()
