"""scripts/pause.sh and scripts/drill.sh, run for real under bash but with
stand-ins for `aws`, `curl` and `sleep`.

The stand-ins are the only things on PATH, so a mistake here cannot reach AWS
or the network: they record what they were given and answer from a small
script of replies. The backend address they hand out ends in .invalid.

At the end, the arithmetic of scripts/coverage.py. Its whole run is not
tested here: it runs these tests, so it would be running itself.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
BASH = shutil.which("bash")

URL = "https://backend.example.invalid/"
ROBIN = "robin-test-token-0123456789"
OWNER = "owner-test-token-9876543210"
TOKENS = {"Robin": ROBIN, "Owner": OWNER}

# Plays aws, curl or sleep. Every call is written down first, then answered
# from files the test put in place: what describe-stacks and get-parameter
# print (no file makes the command fail), and the HTTP replies curl gives, in
# order, the last one repeating. It imports nothing that is not already loaded
# when Python starts, because a drill runs it dozens of times.
STUB = r'''
import os, sys
tool, home, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
def given(name):
    path = os.path.join(home, name)
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        return handle.read()
calls = os.path.join(home, "calls")
earlier = sorted(os.listdir(calls))
with open(os.path.join(calls, "%04d.%s" % (len(earlier), tool)), "w") as handle:
    handle.write("\0".join(argv + [sys.stdin.read() if tool == "curl" else ""]))
if tool == "aws":
    reply = given({"cloudformation": "url", "ssm": "tokens"}[argv[0]])
    if reply is None:
        sys.exit("An error occurred (stand-in): no such thing")
    print(reply)
elif tool == "curl":
    if given("curl_exit"):
        sys.exit(int(given("curl_exit")))
    replies = sorted(os.listdir(os.path.join(home, "replies")))
    asked = len([name for name in earlier if name.endswith(".curl")])
    status, body = given("replies/" + replies[min(asked, len(replies) - 1)]).split("\n", 1)
    sys.stdout.write(body)
    if "-w" in argv:
        wanted = argv[argv.index("-w") + 1]
        sys.stdout.write(wanted.replace("\\n", "\n").replace("%{http_code}", status))
'''


def setUpModule():
    """One folder of stand-ins for the whole file: macOS checks a new
    executable the first time it runs, which costs more than the test."""
    global HOME, BIN
    if not BASH:
        return
    HOME = tempfile.mkdtemp()
    unittest.addModuleCleanup(shutil.rmtree, HOME, ignore_errors=True)
    BIN = os.path.join(HOME, "bin")
    os.mkdir(BIN)
    stub = os.path.join(HOME, "stub.py")
    with open(stub, "w") as handle:
        handle.write(STUB)
    python = shlex.quote(sys.executable)
    commands = {"python3": "exec %s \"$@\"" % python}
    for tool in ("aws", "curl", "sleep"):
        commands[tool] = "exec %s %s %s %s \"$@\"" % (
            python, shlex.quote(stub), tool, shlex.quote(HOME))
    for name, line in commands.items():
        path = os.path.join(BIN, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n%s\n" % line)
        os.chmod(path, 0o755)


@unittest.skipUnless(BASH, "bash is not installed")
class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.home, self.bin = HOME, BIN
        # A stand-in that cannot run would let the real command through on a
        # wider PATH. PATH below is this folder alone, and this makes sure of
        # the other half.
        for name in ("aws", "curl", "sleep", "python3"):
            self.assertTrue(os.access(os.path.join(self.bin, name), os.X_OK), name)

    def run_script(self, script, *args, **settings):
        """Run one script. Settings: url and tokens (None for a lookup that
        fails), replies as (status, body) pairs, curl_exit, and env for extra
        environment variables."""
        for name in ("url", "tokens", "curl_exit"):
            path = os.path.join(self.home, name)
            if os.path.exists(path):
                os.remove(path)
        for folder in ("calls", "replies"):
            shutil.rmtree(os.path.join(self.home, folder), ignore_errors=True)
            os.mkdir(os.path.join(self.home, folder))
        given = {
            "url": settings.get("url", URL),
            "tokens": settings.get("tokens", json.dumps(TOKENS)),
            "curl_exit": settings.get("curl_exit"),
        }
        replies = settings.get("replies", [[200, "{}"]])
        for index, (status, body) in enumerate(replies):
            given["replies/%04d" % index] = "%d\n%s" % (status, body)
        for name, value in given.items():
            if value is not None:
                with open(os.path.join(self.home, name), "w") as handle:
                    handle.write(str(value))
        env = {
            "PATH": self.bin, "HOME": self.home, "TMPDIR": self.home,
            "PYTHONDONTWRITEBYTECODE": "1",
            "ER_STACK": "example-stack", "ER_REGION": "xx-test-1", "ER_PREFIX": "/example",
        }
        # Some CI images need this for their own Python to start; the stub
        # python3 on PATH runs that interpreter.
        if os.environ.get("LD_LIBRARY_PATH"):
            env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
        env.update(settings.get("env", {}))
        done = subprocess.run(
            [BASH, os.path.join(SCRIPTS, script)] + list(args), env=env, cwd=self.home,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=120)
        self.calls = []
        folder = os.path.join(self.home, "calls")
        for record in sorted(os.listdir(folder)):
            with open(os.path.join(folder, record)) as handle:
                parts = handle.read().split("\0")
            self.calls.append({"tool": record.split(".")[1], "argv": parts[:-1], "stdin": parts[-1]})
        return done

    def made(self, tool):
        return [call for call in self.calls if call["tool"] == tool]

    def requests(self):
        """Each curl call as (method, address, JSON body or None)."""
        out = []
        for call in self.made("curl"):
            argv = call["argv"]
            body = json.loads(argv[argv.index("-d") + 1]) if "-d" in argv else None
            out.append((argv[argv.index("-X") + 1], argv[-1], body))
        return out

    def assert_token_kept_off_command_lines(self, done, token=ROBIN):
        self.assertTrue(self.made("curl"), "no request was made")
        for call in self.calls:
            for secret in TOKENS.values():
                self.assertNotIn(secret, " ".join(call["argv"]), call["tool"])
        for call in self.made("curl"):
            self.assertEqual(call["stdin"], "Authorization: Bearer %s\n" % token)
            # The header is read from standard input, and curl is held to https.
            self.assertEqual(call["argv"][call["argv"].index("-H") + 1], "@-")
            self.assertEqual(call["argv"][call["argv"].index("--proto") + 1], "=https")
        for secret in TOKENS.values():
            self.assertNotIn(secret, done.stdout + done.stderr)


class PauseTest(ScriptCase):
    PAUSED = '{"paused": true, "paused_by": "Robin", "seconds": 3600}'

    def test_no_argument_pauses_for_an_hour(self):
        done = self.run_script("pause.sh", replies=[[200, self.PAUSED]])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.requests(), [("POST", URL + "pause", {"minutes": 60})])
        # The reply is printed as it came, on one line.
        self.assertEqual(done.stdout, self.PAUSED + "\n")
        self.assert_token_kept_off_command_lines(done)

    def test_a_number_is_the_minutes(self):
        self.run_script("pause.sh", "240")
        self.assertEqual(self.requests(), [("POST", URL + "pause", {"minutes": 240})])

    def test_a_leading_zero_is_still_decimal(self):
        self.run_script("pause.sh", "090")
        self.assertEqual(self.requests(), [("POST", URL + "pause", {"minutes": 90})])

    def test_off_resumes(self):
        done = self.run_script("pause.sh", "off", replies=[[200, '{"paused": false}']])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.requests(), [("POST", URL + "resume", None)])
        self.assertEqual(done.stdout, '{"paused": false}\n')
        self.assert_token_kept_off_command_lines(done)

    def test_a_bad_argument_is_refused_before_anything_is_looked_up(self):
        for argument in ("abc", "-5", "1.5", "0", "000", "1234567", "on"):
            with self.subTest(argument=argument):
                done = self.run_script("pause.sh", argument)
                self.assertEqual(done.returncode, 2)
                self.assertIn("usage:", done.stderr)
                self.assertEqual(self.calls, [])

    def test_looks_up_the_configured_stack_region_and_prefix(self):
        self.run_script("pause.sh")
        stack, parameter = [call["argv"] for call in self.made("aws")]
        self.assertEqual(stack[:2], ["cloudformation", "describe-stacks"])
        self.assertEqual(stack[stack.index("--stack-name") + 1], "example-stack")
        self.assertEqual(stack[stack.index("--region") + 1], "xx-test-1")
        self.assertEqual(parameter[:2], ["ssm", "get-parameter"])
        self.assertEqual(parameter[parameter.index("--name") + 1], "/example/trigger-tokens")
        self.assertIn("--with-decryption", parameter)

    def test_a_refusal_is_shown_and_fails(self):
        done = self.run_script("pause.sh", replies=[[401, '{"error": "unauthorized"}']])
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, '{"error": "unauthorized"}\n')
        self.assertIn("HTTP 401", done.stderr)

    def test_no_answer_fails(self):
        done = self.run_script("pause.sh", curl_exit=6)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertIn("no answer from the backend", done.stderr)

    def test_a_backend_that_is_not_https_never_gets_the_token(self):
        for url in ("http://backend.example.invalid/", "ftp://backend.example.invalid/",
                    "backend.example.invalid", "https://"):
            with self.subTest(url=url):
                done = self.run_script("pause.sh", url=url)
                self.assertEqual(done.returncode, 1)
                self.assertIn("not https", done.stderr)
                self.assertEqual(self.made("curl"), [])

    def test_a_stack_without_the_output_is_named(self):
        for url in ("None", "", URL + "\t" + URL):
            with self.subTest(url=url):
                done = self.run_script("pause.sh", url=url)
                self.assertEqual(done.returncode, 1)
                self.assertIn("FunctionUrl", done.stderr)
                self.assertIn("example-stack", done.stderr)
                self.assertEqual(self.made("curl"), [])

    def test_a_missing_stack_is_named(self):
        done = self.run_script("pause.sh", url=None)
        self.assertEqual(done.returncode, 1)
        self.assertIn("could not read the stack example-stack in xx-test-1", done.stderr)
        self.assertEqual(self.made("curl"), [])

    def test_an_address_without_a_final_slash_gets_one(self):
        self.run_script("pause.sh", url="https://backend.example.invalid")
        self.assertEqual(self.requests()[0][1], URL + "pause")

    def test_a_missing_token_parameter_is_named(self):
        done = self.run_script("pause.sh", tokens=None)
        self.assertEqual(done.returncode, 1)
        self.assertIn("/example/trigger-tokens", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(self.made("curl"), [])

    def test_a_token_list_that_cannot_be_used_is_not_echoed(self):
        for tokens in ("not json " + ROBIN, "{}", json.dumps([ROBIN]),
                       json.dumps({"Robin": ""}), json.dumps({"Robin": "two words"})):
            with self.subTest(tokens=tokens):
                done = self.run_script("pause.sh", tokens=tokens)
                self.assertEqual(done.returncode, 1)
                self.assertNotIn("Traceback", done.stderr)
                self.assertNotIn(ROBIN, done.stdout + done.stderr)
                self.assertNotIn("two words", done.stdout + done.stderr)
                self.assertEqual(self.made("curl"), [])

    def test_er_sender_chooses_whose_name_the_pause_goes_under(self):
        done = self.run_script("pause.sh", env={"ER_SENDER": "Owner"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_token_kept_off_command_lines(done, token=OWNER)

    def test_an_unknown_er_sender_lists_the_names_and_no_tokens(self):
        done = self.run_script("pause.sh", env={"ER_SENDER": "Sam"})
        self.assertEqual(done.returncode, 1)
        self.assertIn("no sender 'Sam' in trigger-tokens; have: Robin, Owner", done.stderr)
        self.assertNotIn(ROBIN, done.stderr)
        self.assertNotIn(OWNER, done.stderr)
        self.assertEqual(self.made("curl"), [])


class DrillTest(ScriptCase):
    STARTED = [201, json.dumps({"id": "abc123", "status": "active", "summary": "Alerting."})]
    ACTIVE = [200, json.dumps({"id": "abc123", "status": "active", "attempts": 0,
                               "acked_via": None})]
    ACKED = [200, json.dumps({"id": "abc123", "status": "acknowledged", "attempts": 1,
                              "acked_via": "call", "final": "The owner acknowledged."})]

    def test_a_bad_mode_is_refused_before_anything_is_looked_up(self):
        done = self.run_script("drill.sh", "real")
        self.assertEqual(done.returncode, 2)
        self.assertIn("usage:", done.stderr)
        self.assertEqual(self.calls, [])

    def test_a_quiet_drill_is_started_and_followed_until_it_closes(self):
        done = self.run_script("drill.sh", replies=[self.STARTED, self.ACTIVE, self.ACKED])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.requests(), [
            ("POST", URL + "trigger", {
                "kind": "test", "test": "drill",
                "note": "Emergency Ring drill. Acknowledge to stop it."}),
            ("GET", URL + "incidents/abc123", None),
            ("GET", URL + "incidents/abc123", None),
        ])
        self.assertEqual(done.stdout, (
            "started drill abc123\n"
            "t+10s: active | calls: 0 | via: None\n"
            "t+20s: acknowledged | calls: 1 | via: call\n"))
        # It waits ten seconds between looks, and only while still active.
        self.assertEqual([call["argv"] for call in self.made("sleep")], [["10"]])
        self.assert_token_kept_off_command_lines(done)

    def test_a_rehearsal_with_a_note_of_ones_own(self):
        done = self.run_script("drill.sh", "full", 'Robin\'s "note"',
                               replies=[self.STARTED, self.ACKED])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.requests()[0], ("POST", URL + "trigger", {
            "kind": "test", "test": "full", "note": 'Robin\'s "note"'}))
        self.assertTrue(done.stdout.startswith("started full abc123\n"))

    def test_an_incident_that_ends_unanswered_ends_the_wait_as_a_failure(self):
        # The path may work, but nobody confirmed it: only an acknowledgment
        # is a passed drill.
        expired = [200, json.dumps({"status": "expired", "attempts": 5, "acked_via": None})]
        done = self.run_script("drill.sh", replies=[self.STARTED, expired])
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("t+10s: expired | calls: 5 | via: None\n", done.stdout)
        self.assertEqual(self.made("sleep"), [])

    def test_it_says_which_sender_it_runs_as_without_changing_stdout(self):
        done = self.run_script("drill.sh", replies=[self.STARTED, self.ACKED])
        self.assertIn("sending as the sender Robin", done.stderr)
        self.assertTrue(done.stdout.startswith("started drill abc123\n"))

    def test_joining_an_incident_already_active_is_not_a_started_drill(self):
        joined = [200, json.dumps({"id": "abc123", "created": False, "status": "active"})]
        done = self.run_script("drill.sh", replies=[joined])
        self.assertEqual(done.returncode, 1)
        self.assertIn("another incident is already active (abc123)", done.stderr)
        self.assertEqual(done.stdout, "")
        self.assertEqual(len(self.requests()), 1)

    def test_curl_is_told_to_ignore_the_users_curlrc(self):
        self.run_script("drill.sh", replies=[self.STARTED, self.ACKED])
        for made in self.made("curl"):
            self.assertEqual(made["argv"][0], "-q")

    def test_a_reply_without_an_id_is_shown_and_nothing_is_followed(self):
        refusals = (
            [502, '{"error": "no alert has gone out yet", "incident_id": "abc123"}'],
            [503, '{"error": "paused", "retry_after_seconds": 600}'],
            [401, '{"error": "unauthorized"}'],
            [500, "Internal Server Error"],
        )
        for status, body in refusals:
            with self.subTest(status=status):
                done = self.run_script("drill.sh", replies=[[status, body]])
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout, "")
                self.assertIn(body, done.stderr)
                self.assertIn("HTTP %d" % status, done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                self.assertEqual(len(self.made("curl")), 1)

    def test_a_status_that_cannot_be_read_stops_the_wait(self):
        done = self.run_script("drill.sh", replies=[self.STARTED, [404, '{"error": "not found"}']])
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "started drill abc123\n")
        self.assertIn("HTTP 404", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    @unittest.skipUnless(os.environ.get("ER_SLOW_TESTS"),
                         "sixty rounds of processes; set ER_SLOW_TESTS=1 to run it")
    def test_it_gives_up_after_ten_minutes_of_looking(self):
        done = self.run_script("drill.sh", replies=[self.STARTED, self.ACTIVE])
        self.assertEqual(done.returncode, 1)
        self.assertIn("still active after 10 minutes", done.stderr)
        self.assertEqual(len(self.made("curl")), 61)
        self.assertEqual(len(self.made("sleep")), 60)
        self.assertTrue(done.stdout.endswith("t+600s: active | calls: 0 | via: None\n"))

    def test_a_backend_that_is_not_https_never_gets_the_token(self):
        done = self.run_script("drill.sh", url="http://backend.example.invalid/")
        self.assertEqual(done.returncode, 1)
        self.assertIn("not https", done.stderr)
        self.assertEqual(self.made("curl"), [])

    def test_er_sender_chooses_the_token(self):
        done = self.run_script("drill.sh", replies=[self.STARTED, self.ACKED],
                               env={"ER_SENDER": "Owner"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_token_kept_off_command_lines(done, token=OWNER)


def load_coverage_script():
    # By path and under another name: `coverage` is also the name of a
    # well-known package that may be installed, or even running these tests.
    spec = importlib.util.spec_from_file_location(
        "er_coverage_script", os.path.join(SCRIPTS, "coverage.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoverageScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = load_coverage_script()

    def test_line_numbers_are_shown_as_ranges(self):
        self.assertEqual(self.script.ranges([9, 3, 4, 5, 12, 13]), ["3-5", "9", "12-13"])
        self.assertEqual(self.script.ranges([]), [])

    def test_statements_count_and_docstrings_and_comments_do_not(self):
        source = (
            '"""About the module."""\n'          # 1
            "import os\n"                         # 2
            "\n"                                  # 3
            "# a comment\n"                       # 4
            "def join(a,\n"                       # 5
            "         b):\n"                      # 6
            '    """About the function,\n'       # 7
            '    on two lines."""\n'             # 8
            "    if a:\n"                         # 9
            "        return os.path.join(a,\n"    # 10
            "                            b)\n"    # 11
            "    return b\n"                      # 12
        )
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = os.path.join(folder, "example.py")
        with open(path, "w") as handle:
            handle.write(source)
        self.assertEqual(self.script.executable_lines(path), {2, 5, 9, 10, 12})

    def test_it_looks_at_this_repository_and_asks_for_no_minimum_by_default(self):
        self.assertTrue(os.path.samefile(self.script.REPO, os.path.join(SCRIPTS, "..")))
        with self.assertRaises(SystemExit) as stop:
            with open(os.devnull, "w") as quiet, mock.patch.object(sys, "stdout", quiet):
                self.script.main(["--help"])
        self.assertEqual(stop.exception.code, 0)

    def test_the_first_lines_say_how_to_run_it(self):
        with open(os.path.join(SCRIPTS, "coverage.py")) as handle:
            top = handle.read().splitlines()[:2]
        self.assertIn("python3 scripts/coverage.py", top[1])


if __name__ == "__main__":
    unittest.main()
