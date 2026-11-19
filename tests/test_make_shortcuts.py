"""scripts/make_shortcuts.py builds the sender's red button, and nothing else
checks it before it is on a phone.

Two kinds of test. The first plays a generated Shortcut the way the Shortcuts
app would, against the real request handler on fakes, so the keys the
Shortcut reads ("id", "final") are pinned to what the backend sends. The
second runs the script's main() with `aws` and `shortcuts` replaced, and
looks at the files it leaves. Nothing here starts a process or uses a real
secret.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import support
from support import ROBIN

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import make_shortcuts as ms  # noqa: E402

URL = support.BASE + "/"
BACKEND_APP = "backend-app-token-000111222333"
SHORTCUT_APP = "shortcut-app-token-444555666777"
USER_KEY = "pushover-user-key-888999000111"
FAKES = {ROBIN, BACKEND_APP, SHORTCUT_APP, USER_KEY}
PREFIX = "/example"
# A point in open sea, nobody's address.
LAT, LON = 1.25, -20.5


def settings(**changes):
    cfg = {"sender": "Robin", "owner": "Alex", "sound": "persistent", "url": URL,
           "token": ROBIN, "pushover_token": SHORTCUT_APP, "pushover_user": USER_KEY}
    cfg.update(changes)
    return cfg


def as_stored(actions):
    """The actions as they come back out of a .shortcut file."""
    content = ms.workflow(actions, ms.RED, ms.GLYPH)
    return plistlib.loads(plistlib.dumps(content, fmt=plistlib.FMT_BINARY))["WFWorkflowActions"]


def strings(value):
    """Every piece of text in a plist value, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def kind_of(action):
    return action["WFWorkflowActionIdentifier"][len(ms.ACT):]


class Phone:
    """Plays a Shortcut's actions: enough of the Shortcuts app for the ones
    this script uses, and strict about the rest. An action it does not know,
    or a variable nothing has set, fails the test."""

    def __init__(self, actions, choose, http, on_delay=lambda phone: None):
        self.actions, self.choose, self.http, self.on_delay = actions, choose, http, on_delay
        self.outputs, self.named = {}, {}
        self.menus, self.requests, self.shown, self.waited = [], [], [], 0
        # Where each menu, If and Repeat begins, branches and ends.
        self.groups = {}
        for index, action in enumerate(actions):
            group = action["WFWorkflowActionParameters"].get("GroupingIdentifier")
            if group:
                self.groups.setdefault(group, []).append(index)

    # ---- values

    def variable(self, data):
        if data["Type"] == "Variable":
            value = self.named[data["VariableName"]]
        else:
            value = self.outputs[data["OutputUUID"]]
        for step in data.get("Aggrandizements", []):
            value = value[step["PropertyName"]]
        return value

    def value(self, item):
        if not isinstance(item, dict):
            return item
        if item["WFSerializationType"] == "WFTextTokenAttachment":
            return self.variable(item["Value"])
        if item["WFSerializationType"] == "WFDictionaryFieldValue":
            return {self.value(field["WFKey"]): self.value(field["WFValue"])
                    for field in item["Value"]["WFDictionaryFieldValueItems"]}
        text, slots = item["Value"]["string"], item["Value"]["attachmentsByRange"]
        out = []
        for position, character in enumerate(text):
            slot = slots.get("{%d, 1}" % position)
            out.append(character if slot is None else str(self.variable(slot)))
        assert ms.OBJ not in "".join(out), "a variable slot with no variable in it"
        return "".join(out)

    @staticmethod
    def has_value(value):
        return value is not None and value != ""

    # ---- running

    def run(self):
        repeats = {}
        at = 0
        while at < len(self.actions):
            action = self.actions[at]
            what, p = kind_of(action), action["WFWorkflowActionParameters"]
            mode, marks = p.get("WFControlFlowMode"), self.groups.get(p.get("GroupingIdentifier"))
            at += 1
            if what == "exit":
                return self
            elif what == "choosefrommenu" and mode == 0:
                self.menus.append((p["WFMenuPrompt"], p["WFMenuItems"]))
                picked = self.choose(p["WFMenuPrompt"], p["WFMenuItems"])
                at = 1 + next(i for i in marks[1:-1]
                              if self.actions[i]["WFWorkflowActionParameters"]["WFMenuItemTitle"] == picked)
            elif what == "conditional" and mode == 0:
                assert p["WFCondition"] == 100, "only 'has any value' is understood here"
                if not self.has_value(self.value(p["WFInput"]["Variable"])):
                    at = marks[1] + 1
            elif what in ("choosefrommenu", "conditional") and mode == 1:
                at = marks[-1] + 1      # the end of the branch that ran
            elif what == "repeat.count" and mode == 0:
                repeats[marks[0]] = p["WFRepeatCount"]
            elif what == "repeat.count" and mode == 2:
                repeats[marks[0]] -= 1
                if repeats[marks[0]]:
                    at = marks[0] + 1
            elif mode == 2:
                pass
            elif what == "gettext":
                self.outputs[p["UUID"]] = self.value(p["WFTextActionText"])
            elif what == "setvariable":
                self.named[p["WFVariableName"]] = self.value(p["WFInput"])
            elif what == "getvalueforkey":
                source = self.value(p["WFInput"])
                self.outputs[p["UUID"]] = (
                    source.get(p["WFDictionaryKey"]) if isinstance(source, dict) else None)
            elif what == "getcurrentlocation":
                self.outputs[p["UUID"]] = {"Latitude": LAT, "Longitude": LON}
            elif what == "downloadurl":
                body_type = p.get("WFHTTPBodyType")
                body = p.get({"JSON": "WFJSONValues", "Form": "WFFormValues"}.get(body_type))
                request = {
                    "method": p["WFHTTPMethod"], "url": self.value(p["WFURL"]),
                    "headers": self.value(p["WFHTTPHeaders"]) if "WFHTTPHeaders" in p else {},
                    "type": body_type, "body": self.value(body) if body else None,
                }
                self.requests.append(request)
                self.outputs[p["UUID"]] = self.http(request)
            elif what == "notification":
                self.shown.append(("notification", p["WFNotificationActionTitle"],
                                   self.value(p["WFNotificationActionBody"])))
            elif what == "alert":
                self.shown.append(("alert", p["WFAlertActionTitle"],
                                   self.value(p["WFAlertActionMessage"])))
            elif what == "delay":
                self.waited += p["WFDelayTime"]
                self.on_delay(self)
            else:
                raise AssertionError("this test does not know the action %r" % what)
        return self


class ShortcutCase(unittest.TestCase):
    """A sender's phone in front of the real handler on fakes."""

    def setUp(self):
        logging.disable(logging.CRITICAL)   # refusals are logged on purpose
        self.addCleanup(logging.disable, logging.NOTSET)
        os.environ.pop("PUBLIC_BASE_URL", None)
        self.fresh()

    def fresh(self):
        """A backend with no history, and a Pushover that accepts."""
        self.sim = support.make()
        self.pushover = []
        self.pushover_reply = {"status": 1, "receipt": "r-example"}
        self.backend_replies = []

    def http(self, request):
        if request["url"] == ms.PUSHOVER:
            self.pushover.append(request)
            return self.pushover_reply
        self.assertTrue(request["url"].startswith(URL), request["url"])
        self.assertEqual(request["type"], "JSON" if request["method"] == "POST" else None)
        response = support.request(
            request["method"], "/" + request["url"][len(URL):],
            body=request["body"], headers=request["headers"])
        try:
            reply = json.loads(response["body"])
        except ValueError:
            reply = response["body"]
        self.backend_replies.append((response["statusCode"], reply))
        return reply

    def play(self, actions, choice="Kids", on_delay=lambda phone: None):
        return Phone(as_stored(actions), lambda prompt, items: choice, self.http, on_delay).run()

    def incident(self):
        return self.sim.store.get(next(iter(self.sim.store.items)))

    def acknowledge(self, phone=None):
        """The owner acknowledges the push, as the Pushover app would report it."""
        incident = self.incident()
        if incident.status == "active":
            self.sim.notifier.acknowledged.add(incident.pushover_receipt)
            self.sim.service.acknowledge_from_push(incident.id, incident.pushover_receipt)


class EmergencyTest(ShortcutCase):
    def test_the_menu_is_the_only_thing_before_sending_and_can_be_backed_out_of(self):
        phone = self.play(ms.emergency(settings()), choice="Cancel")
        self.assertEqual(phone.menus, [("What is the emergency?", ["Kids", "Me", "House", "Cancel"])])
        self.assertEqual(phone.requests, [])
        self.assertEqual(phone.shown, [])
        self.assertEqual(self.sim.store.items, {})

    def test_each_choice_starts_a_real_incident_of_that_kind_with_the_location(self):
        for choice in ("Kids", "Me", "House"):
            with self.subTest(choice=choice):
                self.fresh()
                phone = self.play(ms.emergency(settings()), choice, on_delay=self.acknowledge)
                first = phone.requests[0]
                self.assertEqual((first["method"], first["url"]), ("POST", URL + "trigger"))
                self.assertEqual(first["headers"], {"Authorization": "Bearer " + ROBIN})
                self.assertEqual(first["body"], {"kind": choice.lower(), "lat": str(LAT), "lon": str(LON)})
                incident = self.incident()
                self.assertEqual((incident.sender, incident.kind), ("Robin", choice.lower()))
                self.assertEqual((float(incident.lat), float(incident.lon)), (LAT, LON))
                self.assertFalse(incident.test)
                self.assertEqual(self.pushover, [])

    def test_an_acknowledged_alert_shows_the_backends_closing_sentence(self):
        phone = self.play(ms.emergency(settings()), on_delay=self.acknowledge)
        (status, started), (_, closed) = self.backend_replies
        self.assertEqual(status, 201)
        # The two keys the Shortcut decides by.
        self.assertIn("id", started)
        self.assertNotIn("final", started)
        self.assertEqual(closed["final"], closed["summary"])
        self.assertEqual(phone.requests[1]["method"], "GET")
        self.assertEqual(phone.requests[1]["url"], URL + "incidents/" + started["id"])
        self.assertEqual(phone.requests[1]["headers"], {"Authorization": "Bearer " + ROBIN})
        self.assertEqual(phone.shown, [
            ("notification", "Emergency Ring", "Alert sent. Alex's phone is sounding."),
            ("alert", "Emergency Ring", closed["final"]),
        ])
        self.assertEqual(phone.waited, 15)

    def test_no_acknowledgment_is_said_after_three_minutes_of_looking(self):
        phone = self.play(ms.emergency(settings()))
        self.assertEqual(phone.waited, 180)
        self.assertEqual(len(phone.requests), 1 + 12)
        for status, reply in self.backend_replies[1:]:
            self.assertEqual(status, 200)
            self.assertNotIn("final", reply)
        kind, title, message = phone.shown[-1]
        self.assertEqual((kind, title), ("alert", "No response yet"))
        self.assertIn("Alex's phone keeps alerting", message)
        self.assertEqual(self.incident().status, "active")
        self.assertEqual(self.pushover, [])

    def test_what_the_sender_reads_makes_no_promise_about_calls_or_timing(self):
        # Which arms are on is the deployment's choice, and can change later.
        for cfg in (settings(), settings(pushover_user=None, pushover_token=None)):
            for text in strings(as_stored(ms.emergency(cfg))):
                self.assertNotIn("calls start", text)
                self.assertNotIn("30 seconds", text)

    # ---- the fallback: any reply without an "id"

    def assert_fell_back(self, phone, kind="kids"):
        self.assertEqual(len(phone.requests), 2)
        sent = self.pushover[0]
        self.assertEqual((sent["method"], sent["type"]), ("POST", "Form"))
        self.assertEqual(sent["headers"], {})
        self.assertEqual(sent["body"], {
            "token": SHORTCUT_APP, "user": USER_KEY,
            "title": "EMERGENCY from Robin (backup)",
            "message": "Emergency from Robin: %s. Call me now. "
                       "https://maps.apple.com/?ll=%s,%s" % (kind, LAT, LON),
            "priority": "2", "retry": "30", "expire": "1800", "sound": "persistent",
        })
        self.assertEqual(phone.shown, [("alert", "Backup alert sent", "Now phone Alex.")])
        self.assertEqual(phone.waited, 0)

    def test_falls_back_when_alerts_are_paused(self):
        support.call("POST", "/pause")
        phone = self.play(ms.emergency(settings()))
        status, reply = self.backend_replies[-1]
        self.assertEqual(status, 503)
        self.assertNotIn("id", reply)
        self.assert_fell_back(phone)

    def test_falls_back_when_the_first_alert_did_not_go_out(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError("vendor unavailable")
        with mock.patch.object(self.sim.notifier, "send_emergency", unavailable):
            phone = self.play(ms.emergency(settings()), choice="House")
        status, reply = self.backend_replies[-1]
        # The incident exists, and its id is there for a person to read, but
        # under another name so that the Shortcut does not take it for success.
        self.assertEqual(status, 502)
        self.assertIn("incident_id", reply)
        self.assertNotIn("id", reply)
        self.assert_fell_back(phone, kind="house")

    def test_falls_back_when_the_token_is_refused(self):
        phone = self.play(ms.emergency(settings(token="not-the-token-0123456789")))
        self.assertEqual(self.backend_replies[-1][0], 401)
        self.assertEqual(self.sim.store.items, {})
        self.assert_fell_back(phone)

    def test_falls_back_when_the_reply_is_not_json(self):
        self.http = lambda request: (
            self.pushover.append(request) or self.pushover_reply
            if request["url"] == ms.PUSHOVER else "Service Unavailable")
        self.assert_fell_back(self.play(ms.emergency(settings())))

    def test_says_so_when_the_fallback_fails_too(self):
        support.call("POST", "/pause")
        self.pushover_reply = {"status": 0, "errors": ["application token is invalid"]}
        phone = self.play(ms.emergency(settings()))
        self.assertEqual(len(self.pushover), 1)
        self.assertEqual(phone.shown, [("alert", "Alert NOT sent", "Phone Alex now.")])

    def test_without_pushover_the_fallback_is_to_say_phone_them(self):
        support.call("POST", "/pause")
        phone = self.play(ms.emergency(settings(pushover_user=None, pushover_token=None)))
        self.assertEqual(len(phone.requests), 1)
        self.assertEqual(self.pushover, [])
        self.assertEqual(phone.shown, [("alert", "Alert NOT sent", "Phone Alex now.")])

    def test_the_sound_of_the_fallback_is_the_one_asked_for(self):
        support.call("POST", "/pause")
        self.play(ms.emergency(settings(sound="siren")))
        self.assertEqual(self.pushover[0]["body"]["sound"], "siren")


class DrillTest(ShortcutCase):
    def test_a_drill_is_sent_as_a_test_and_says_so_in_its_menu(self):
        phone = self.play(ms.emergency(settings(), test="drill"), on_delay=self.acknowledge)
        self.assertEqual(phone.menus[0][0], "DRILL: what is the emergency?")
        self.assertEqual(phone.requests[0]["body"]["test"], "drill")
        incident = self.incident()
        # The backend reads "drill" as it reads true: a quiet, labelled test
        # with the test sound, not a rehearsal with the real one.
        self.assertEqual((incident.test, incident.rehearsal), (True, False))
        self.fresh()
        _, quiet = support.call("POST", "/trigger", {"kind": "kids", "test": True})
        self.assertEqual((quiet["test"], quiet["rehearsal"]), (True, False))

    def test_a_drill_that_cannot_start_sends_nothing_to_pushover(self):
        # Sent straight to Pushover it would be a real, unlabelled emergency.
        support.call("POST", "/pause", {"minutes": 5})
        paused = self.play(ms.emergency(settings(), test="drill"))
        self.assertEqual(self.backend_replies[-1][0], 503)
        self.fresh()
        refused = self.play(ms.emergency(settings(token="not-the-token-0123456789"), test="drill"))
        self.assertEqual(self.backend_replies[-1][0], 401)
        self.assertEqual(self.pushover, [])
        for phone in (paused, refused):
            self.assertEqual(len(phone.requests), 1)
            self.assertEqual(phone.shown[-1][:2], ("alert", "Drill not started"))

    def test_a_drill_whose_first_alert_fails_is_followed_not_escalated(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError("vendor unavailable")
        with mock.patch.object(self.sim.notifier, "send_emergency", unavailable):
            # A pause ends the drill, so the Shortcut's wait ends too.
            self.play(ms.emergency(settings(), test="drill"),
                      on_delay=lambda phone: support.call("POST", "/pause", {"minutes": 1}))
        self.assertEqual(self.backend_replies[0][0], 201)
        self.assertEqual(self.pushover, [])

    def test_the_drill_file_carries_no_pushover_keys(self):
        stored = "\n".join(strings(as_stored(ms.emergency(settings(), test="drill"))))
        self.assertNotIn(USER_KEY, stored)
        self.assertNotIn(SHORTCUT_APP, stored)
        self.assertIn(USER_KEY, "\n".join(strings(as_stored(ms.emergency(settings())))))

    def test_the_real_one_carries_no_test_marker(self):
        phone = self.play(ms.emergency(settings()), on_delay=self.acknowledge)
        self.assertNotIn("test", phone.requests[0]["body"])


class BackupTest(ShortcutCase):
    def test_it_alerts_through_pushover_alone(self):
        phone = self.play(ms.backup(settings()))
        self.assertEqual(phone.menus, [])
        self.assertEqual(len(phone.requests), 1)
        self.assertEqual(self.pushover[0]["body"], {
            "token": SHORTCUT_APP, "user": USER_KEY,
            "title": "EMERGENCY from Robin (backup)",
            "message": "Emergency from Robin. Call me now.",
            "priority": "2", "retry": "30", "expire": "1800", "sound": "persistent",
        })
        self.assertEqual(phone.shown, [("alert", "Backup alert sent", "Now phone Alex.")])
        # It never needs the backend, so it never carries the backend's token.
        self.assertNotIn(ROBIN, "\n".join(strings(as_stored(ms.backup(settings())))))

    def test_it_says_so_when_pushover_refuses(self):
        self.pushover_reply = "Bad Gateway"
        phone = self.play(ms.backup(settings()))
        self.assertEqual(phone.shown, [("alert", "Alert NOT sent", "Phone Alex now.")])


SECRET_LIKE = re.compile(r"[A-Za-z0-9_-]{20,}")
UUID = re.compile(r"^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}$")


class WhereTheSecretsAre(unittest.TestCase):
    def setUp(self):
        self.files = {name: plistlib.loads(plistlib.dumps(content, fmt=plistlib.FMT_BINARY))
                      for name, content in ms.shortcuts(settings()).items()}

    def test_the_three_shortcuts(self):
        self.assertEqual(list(self.files), ["Emergency", "Emergency Backup", "Emergency Drill"])
        for content in self.files.values():
            self.assertEqual(content["WFWorkflowImportQuestions"], [])
            self.assertTrue(content["WFWorkflowActions"])

    def test_only_the_drill_file_is_a_drill(self):
        sent = {}
        for name, content in self.files.items():
            phone = Phone(content["WFWorkflowActions"], lambda prompt, items: "Kids",
                          lambda request: {}).run()
            sent[name] = phone.requests[0]["body"].get("test")
        self.assertEqual(sent, {"Emergency": None, "Emergency Backup": None,
                                "Emergency Drill": "drill"})
        # And it does not look like the real one on the Home Screen.
        colours = {name: content["WFWorkflowIcon"]["WFWorkflowIconStartColor"]
                   for name, content in self.files.items()}
        self.assertEqual(colours["Emergency"], colours["Emergency Backup"])
        self.assertNotEqual(colours["Emergency"], colours["Emergency Drill"])

    def test_nothing_that_looks_like_a_secret_but_the_ones_put_in(self):
        for name, content in self.files.items():
            for text in strings(content):
                for found in SECRET_LIKE.findall(text):
                    looks_secret = any(c.isdigit() for c in found) and not UUID.match(found)
                    if looks_secret:
                        self.assertIn(found, FAKES, "%s holds %r" % (name, found))
        self.assertNotIn(BACKEND_APP, "\n".join(strings(self.files)))

    def test_each_secret_sits_only_in_the_request_it_belongs_to(self):
        for name, content in self.files.items():
            for action in content["WFWorkflowActions"]:
                params = action["WFWorkflowActionParameters"]
                held = {fake for fake in FAKES if fake in "\n".join(strings(action))}
                if not held:
                    continue
                self.assertEqual(kind_of(action), "downloadurl", name)
                url = params["WFURL"]
                url = url if isinstance(url, str) else url["Value"]["string"]
                if held == {ROBIN}:
                    # Only ever to the backend, and only as the bearer header.
                    self.assertTrue(url.startswith(URL), url)
                    header = params["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
                    self.assertEqual(
                        [(f["WFKey"]["Value"]["string"], f["WFValue"]["Value"]["string"]) for f in header],
                        [("Authorization", "Bearer " + ROBIN)])
                    body = params.get("WFJSONValues", {})
                    self.assertNotIn(ROBIN, "\n".join(strings(body)))
                else:
                    self.assertEqual(held, {SHORTCUT_APP, USER_KEY})
                    self.assertEqual(url, ms.PUSHOVER)
                    self.assertNotIn("WFHTTPHeaders", params)

    def test_every_request_goes_to_the_backend_or_to_pushover_over_https(self):
        for content in self.files.values():
            for action in content["WFWorkflowActions"]:
                if kind_of(action) == "downloadurl":
                    url = action["WFWorkflowActionParameters"]["WFURL"]
                    url = url if isinstance(url, str) else url["Value"]["string"]
                    self.assertTrue(url.startswith(URL) or url == ms.PUSHOVER, url)
                    self.assertTrue(url.startswith("https://"))


class FakeTools:
    """Stands in for subprocess.run: answers `aws` from a table, plays
    `shortcuts sign` by copying the file, and fails the test on anything else."""

    def __init__(self, parameters, url=URL):
        self.parameters, self.url = parameters, url
        self.push_sound = "persistent"
        self.commands = []
        self.sign_error = None
        self.aws_error = None

    def __call__(self, argv, **options):
        argv = list(argv)
        self.commands.append(argv)
        done = subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "aws":
            if self.aws_error:
                raise self.aws_error
            if argv[1:3] == ["cloudformation", "describe-stacks"]:
                wanted = argv[argv.index("--query") + 1]
                done.stdout = (self.push_sound if "PushSound" in wanted else self.url) + "\n"
                return done
            if argv[1:3] == ["ssm", "get-parameter"]:
                name = argv[argv.index("--name") + 1]
                if name not in self.parameters:
                    raise subprocess.CalledProcessError(254, argv, output="", stderr=(
                        "\nAn error occurred (ParameterNotFound) when calling the "
                        "GetParameter operation: \n"))
                done.stdout = self.parameters[name] + "\n"
                return done
        if argv[:2] == ["shortcuts", "sign"]:
            if self.sign_error:
                raise self.sign_error
            source, target = argv[argv.index("--input") + 1], argv[argv.index("--output") + 1]
            shutil.copyfile(source, target)
            os.chmod(target, 0o644)     # looser than the script should leave it
            return done
        raise AssertionError("the script ran something unexpected: %r" % argv)

    def read(self):
        return [argv[argv.index("--name") + 1] for argv in self.commands if "--name" in argv]


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class MainTest(unittest.TestCase):
    def setUp(self):
        # main() tightens the umask for the whole process; put it back after.
        self.addCleanup(os.umask, os.umask(0o022))
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.out = os.path.join(self.home, "out", "shortcuts")
        self.tools = FakeTools({
            PREFIX + "/trigger-tokens": json.dumps({"Robin": ROBIN, "Sam": "sam-test-token-0123456789"}),
            PREFIX + "/pushover-app-token": BACKEND_APP,
            PREFIX + "/pushover-user-key": USER_KEY,
            PREFIX + "/shortcut-pushover-app-token": SHORTCUT_APP,
        })
        patcher = mock.patch.object(ms.subprocess, "run", self.tools)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *extra, **options):
        """Run main(); returns (exit status or None, stdout, stderr)."""
        argv = ["--sender", options.get("sender", "Robin"), "--owner", "Alex",
                "--out", self.out, "--prefix", PREFIX, "--stack", "example-stack",
                "--region", "xx-test-1"] + list(extra)
        out, err, status = io.StringIO(), io.StringIO(), None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ms.main(argv)
            except SystemExit as stop:
                status = stop.code
                if isinstance(status, str):
                    err.write(status + "\n")
        self.said = out.getvalue() + err.getvalue()
        for fake in FAKES:
            self.assertNotIn(fake, self.said)
        return status, out.getvalue(), err.getvalue()

    def files(self):
        return sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []

    def text_of(self, name):
        with open(os.path.join(self.out, name), "rb") as handle:
            return "\n".join(strings(plistlib.load(handle)))

    # ---- what is written, and who can read it

    def test_writes_three_signed_files_only_the_owner_can_read(self):
        status, out, err = self.main()
        self.assertIsNone(status, err)
        self.assertEqual(self.files(), ["Emergency Backup.shortcut", "Emergency Drill.shortcut",
                                        "Emergency.shortcut"])
        for name in self.files():
            self.assertEqual(mode(os.path.join(self.out, name)), 0o600, name)
            self.assertIn("wrote %s" % os.path.join(self.out, name), out)
        self.assertEqual(mode(self.out), 0o700)
        self.assertEqual(mode(os.path.dirname(self.out)), 0o700)
        text = self.text_of("Emergency.shortcut")
        self.assertIn(URL + "trigger", text)
        self.assertIn("Bearer " + ROBIN, text)
        # Each was signed for anyone, since the phone is another person's.
        signing = [argv for argv in self.tools.commands if argv[0] == "shortcuts"]
        self.assertEqual(len(signing), 3)
        for argv in signing:
            self.assertEqual(argv[:4], ["shortcuts", "sign", "--mode", "anyone"])

    def test_reads_the_stack_and_parameters_it_was_pointed_at(self):
        self.main()
        stack = self.tools.commands[0]
        self.assertEqual(stack[stack.index("--stack-name") + 1], "example-stack")
        for argv in self.tools.commands:
            if argv[0] == "aws":
                self.assertEqual(argv[argv.index("--region") + 1], "xx-test-1")

    def test_the_closing_words_say_what_to_delete(self):
        _, out, _ = self.main()
        closing = out[out.index("AirDrop"):]
        self.assertIn("AirDrop these to Robin's iPhone", closing)
        self.assertIn("delete the folder %s" % self.out, closing)
        self.assertIn("delete any copy you imported into Shortcuts on this Mac", closing)

    def test_unsigned_keeps_the_unsigned_files_just_as_private(self):
        status, out, _ = self.main("--unsigned")
        self.assertIsNone(status)
        self.assertEqual(self.files(), [
            "Emergency Backup.unsigned.shortcut", "Emergency Drill.unsigned.shortcut",
            "Emergency.unsigned.shortcut"])
        for name in self.files():
            self.assertEqual(mode(os.path.join(self.out, name)), 0o600, name)
        self.assertFalse([argv for argv in self.tools.commands if argv[0] == "shortcuts"])
        self.assertIn("Bearer " + ROBIN, self.text_of("Emergency.unsigned.shortcut"))
        self.assertIn("delete the folder %s" % self.out, out)
        self.assertNotIn("AirDrop", out)

    def test_files_left_by_an_earlier_run_do_not_keep_their_permissions(self):
        os.makedirs(self.out)
        for name in ("Emergency.shortcut", "Emergency.unsigned.shortcut"):
            path = os.path.join(self.out, name)
            with open(path, "w") as handle:
                handle.write("old")
            os.chmod(path, 0o644)
        self.main()
        self.assertEqual(mode(os.path.join(self.out, "Emergency.shortcut")), 0o600)
        self.assertNotIn("Emergency.unsigned.shortcut", self.files())
        self.main("--unsigned")
        self.assertEqual(mode(os.path.join(self.out, "Emergency.unsigned.shortcut")), 0o600)

    def test_a_failed_signing_leaves_no_unsigned_file_behind(self):
        self.tools.sign_error = subprocess.CalledProcessError(
            1, ["shortcuts"], output="", stderr="Error: could not sign\n")
        status, _, err = self.main()
        self.assertTrue(status)
        self.assertIn("could not sign Emergency.shortcut: Error: could not sign", err)
        self.assertEqual(self.files(), [])

    def test_a_machine_without_the_signing_tool_is_told_about_unsigned(self):
        self.tools.sign_error = FileNotFoundError("shortcuts")
        status, _, err = self.main()
        self.assertTrue(status)
        self.assertIn("--unsigned", err)
        self.assertEqual(self.files(), [])

    def test_placeholder_needs_no_aws(self):
        status, _, _ = self.main("--placeholder")
        self.assertIsNone(status)
        self.assertEqual(len(self.files()), 3)
        self.assertFalse([argv for argv in self.tools.commands if argv[0] == "aws"])
        # Named so that nobody mistakes them for working buttons.
        self.assertTrue(all(name.startswith("PLACEHOLDER ") for name in self.files()))
        text = self.text_of("PLACEHOLDER Emergency.shortcut")
        self.assertIn("https://example.invalid/trigger", text)
        self.assertIn("PLACEHOLDER, alerts nobody.", text)
        for fake in FAKES:
            self.assertNotIn(fake, text)

    # ---- which Pushover token goes onto the phone

    def test_the_shortcuts_own_pushover_token_is_used_when_there_is_one(self):
        _, out, err = self.main()
        for name in self.files():
            self.assertNotIn(BACKEND_APP, self.text_of(name))
        self.assertIn(SHORTCUT_APP, self.text_of("Emergency.shortcut"))
        self.assertIn(SHORTCUT_APP, self.text_of("Emergency Backup.shortcut"))
        self.assertNotIn("warning", out + err)
        # The backend's token is not even fetched.
        self.assertNotIn(PREFIX + "/pushover-app-token", self.tools.read())

    def test_without_one_the_backends_token_is_used_and_that_is_said(self):
        del self.tools.parameters[PREFIX + "/shortcut-pushover-app-token"]
        status, _, err = self.main()
        self.assertIsNone(status)
        self.assertIn(BACKEND_APP, self.text_of("Emergency.shortcut"))
        self.assertIn("warning: no %s/shortcut-pushover-app-token" % PREFIX, err)
        self.assertIn("backend's own Pushover application token", err)

    def test_a_parameter_can_be_named_for_it(self):
        self.tools.parameters["/elsewhere/app-token"] = "elsewhere-app-token-121212121212"
        FAKES.add("elsewhere-app-token-121212121212")
        self.addCleanup(FAKES.discard, "elsewhere-app-token-121212121212")
        status, _, err = self.main("--pushover-token-param", "/elsewhere/app-token")
        self.assertIsNone(status, err)
        text = self.text_of("Emergency.shortcut")
        self.assertIn("elsewhere-app-token-121212121212", text)
        self.assertNotIn(SHORTCUT_APP, text)
        self.assertNotIn(BACKEND_APP, text)

    def test_a_named_parameter_that_is_missing_stops_the_build(self):
        # Quietly using the backend's token would be the opposite of what was asked.
        status, _, err = self.main("--pushover-token-param", "/elsewhere/missing")
        self.assertTrue(status)
        self.assertIn("there is no SSM parameter /elsewhere/missing in xx-test-1", err)
        self.assertEqual(self.files(), [])

    def test_the_backup_sound_is_the_stacks_own_unless_one_is_asked_for(self):
        # So the backup alert sounds like the real one, whatever the stack uses.
        self.tools.push_sound = "siren"
        _, out, _ = self.main("--unsigned")
        self.assertIn("backup alert sound: siren", out)
        with open(os.path.join(self.out, "Emergency Backup.unsigned.shortcut"), "rb") as handle:
            actions = plistlib.load(handle)["WFWorkflowActions"]
        phone = Phone(actions, None, lambda request: {"receipt": "r-example"}).run()
        self.assertEqual(phone.requests[0]["body"]["sound"], "siren")
        # A stack that does not say gets Pushover's long alarm.
        self.tools.push_sound = "None"
        self.main("--unsigned")
        with open(os.path.join(self.out, "Emergency Backup.unsigned.shortcut"), "rb") as handle:
            actions = plistlib.load(handle)["WFWorkflowActions"]
        phone = Phone(actions, None, lambda request: {"receipt": "r-example"}).run()
        self.assertEqual(phone.requests[0]["body"]["sound"], "persistent")
        self.main("--unsigned", "--sound", "siren")
        with open(os.path.join(self.out, "Emergency Backup.unsigned.shortcut"), "rb") as handle:
            actions = plistlib.load(handle)["WFWorkflowActions"]
        phone = Phone(actions, None, lambda request: {"receipt": "r-example"}).run()
        self.assertEqual(phone.requests[0]["body"]["sound"], "siren")

    # ---- deployments with one arm

    def test_a_deployment_without_pushover_gets_two_shortcuts_and_is_told_why(self):
        for name in ("/pushover-app-token", "/pushover-user-key", "/shortcut-pushover-app-token"):
            del self.tools.parameters[PREFIX + name]
        status, _, err = self.main()
        self.assertIsNone(status, err)
        self.assertEqual(self.files(), ["Emergency Drill.shortcut", "Emergency.shortcut"])
        self.assertIn("no Pushover fallback", err)
        self.assertIn("Emergency Backup is not built", err)
        for name in self.files():
            self.assertNotIn("pushover", self.text_of(name).lower())
            self.assertIn("Bearer " + ROBIN, self.text_of(name))

    def test_a_push_only_deployment_builds_all_three(self):
        # It has no Twilio parameter, and the script never asks for one.
        status, _, _ = self.main()
        self.assertIsNone(status)
        self.assertEqual(len(self.files()), 3)
        self.assertEqual([name for name in self.tools.read() if "twilio" in name], [])

    # ---- refusing to build around something wrong

    def test_an_address_that_is_not_the_backend_is_refused_before_any_secret_is_read(self):
        for url in ("None", "", "http://backend.example.invalid/", "https://nodots/",
                    "https://a.example.invalid/ https://b.example.invalid/",
                    "https://user@backend.example.invalid/", "https://backend.example.invalid/?x=1",
                    "https://backend.example.invalid:port/", "backend.example.invalid"):
            with self.subTest(url=url):
                self.tools.url = url
                self.tools.commands = []
                status, _, err = self.main()
                self.assertTrue(status)
                self.assertIn("the stack example-stack has no usable FunctionUrl output", err)
                self.assertEqual(self.tools.read(), [])
                self.assertEqual(self.files(), [])

    def test_an_address_without_a_final_slash_gets_one(self):
        self.tools.url = URL.rstrip("/")
        self.main()
        self.assertIn(URL + "trigger", self.text_of("Emergency.shortcut"))
        self.assertEqual(ms.backend_url(URL, "example-stack"), URL)

    def test_an_unknown_sender_gets_the_names_and_no_tokens(self):
        status, _, err = self.main(sender="Jo")
        self.assertTrue(status)
        self.assertIn("no sender 'Jo' in trigger-tokens; have: Robin, Sam", err)
        self.assertNotIn("sam-test-token", err)
        self.assertEqual(self.files(), [])

    def test_a_token_list_that_is_not_one_is_not_echoed(self):
        for value in ("not json " + ROBIN, json.dumps([ROBIN]), json.dumps({"Robin": ""}),
                      json.dumps({"Robin": {"token": ROBIN}})):
            with self.subTest(value=value):
                self.tools.parameters[PREFIX + "/trigger-tokens"] = value
                status, _, err = self.main()
                self.assertTrue(status)
                self.assertIn("trigger-tokens", err)
                self.assertEqual(self.files(), [])

    def test_a_missing_token_list_is_named(self):
        del self.tools.parameters[PREFIX + "/trigger-tokens"]
        status, _, err = self.main()
        self.assertTrue(status)
        self.assertIn("there is no SSM parameter %s/trigger-tokens in xx-test-1" % PREFIX, err)

    def test_an_aws_failure_shows_what_aws_said_and_not_what_it_printed(self):
        self.tools.aws_error = subprocess.CalledProcessError(
            255, ["aws"], output="value " + ROBIN, stderr="\nUnable to locate credentials.\n")
        status, _, err = self.main()
        self.assertTrue(status)
        self.assertIn("aws cloudformation describe-stacks failed: Unable to locate credentials.", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.files(), [])

    def test_a_machine_without_the_aws_cli_is_told_so(self):
        self.tools.aws_error = FileNotFoundError("aws")
        status, _, err = self.main()
        self.assertTrue(status)
        self.assertIn("the aws command was not found", err)

    # ---- the help

    def test_every_option_is_explained_in_the_help(self):
        parser = ms.parser()
        text = parser.format_help()
        for action in parser._actions:
            self.assertTrue(action.help, action.option_strings)
            self.assertIn(action.option_strings[-1], text)
        for flag in ("--sender", "--owner", "--out", "--sound", "--stack", "--region", "--prefix",
                     "--pushover-token-param", "--placeholder", "--unsigned"):
            self.assertIn(flag, text)
        self.assertIsNone(parser.get_default("sound"))    # read from the stack
        self.assertEqual(parser.get_default("out"), "build/shortcuts")


if __name__ == "__main__":
    unittest.main()
