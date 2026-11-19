"""Keeps the docs honest. A stranger sets this up from the README and docs/,
so their tables are checked against the code they describe: the routes, who may call
them, the template's parameters with their defaults and ranges, and the names
of the secrets. Links must lead somewhere. And nothing shaped like a real
address, account or key may slip into any file that is published.

template.yaml is read as text: the standard library has no YAML parser.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import unittest
from typing import Dict, List

import support  # noqa: F401  (puts src/ on the import path)
from er import handler, ssm_secrets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Stand-ins for the id in a Function URL. Anything else in front of
# ".lambda-url." may be somebody's real address.
PLACEHOLDER_HOSTS = {"abc123", "xxxxxxxx"}
FUNCTION_URL_HOST = re.compile(r"([\w-]+)\.lambda-url\.")
# A Twilio account id: AC and 32 hex digits. The placeholder used in this
# repository is AC and 32 x's, which is not hex and so does not match.
TWILIO_ACCOUNT_ID = re.compile(r"(?<![A-Za-z0-9])AC[0-9a-fA-F]{32}(?![0-9a-fA-F])")
# An AWS access key id, long-lived or temporary.
AWS_ACCESS_KEY_ID = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
# How the "Who may call" column of docs/api.md must begin for each kind of route.
WHO = {
    handler.SENDER: "Sender token", handler.AGENT: "Agent token",
    handler.ANYONE: "Either token", None: "No token",
}


def read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def markdown_files() -> List[str]:
    """Every Markdown file that is published. *.local.md is gitignored: it
    holds one deployment's private notes, and is not opened here."""
    found = glob.glob(os.path.join(ROOT, "*.md")) + glob.glob(os.path.join(ROOT, "docs", "*.md"))
    return sorted(path for path in found if not path.endswith(".local.md"))


def published_files() -> Dict[str, str]:
    """{path: text} for every text file that git tracks here, or would track
    once added. Files that .gitignore names, such as one deployment's private
    notes and settings, are left out and never opened.

    Without git, or outside a repository, there is no list to go by and the
    test that asked is skipped."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        raise unittest.SkipTest("git is not available, or this folder is not a repository")
    files = {}
    for name in filter(None, listed.split(b"\0")):
        path = os.fsdecode(name)
        try:
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                files[path] = handle.read()
        except (OSError, UnicodeDecodeError):
            continue  # deleted but not yet committed, or not text
    return files


def table_rows(text: str) -> List[List[str]]:
    """The cells of every table row. Callers pick out the rows they want by
    the shape of the first cell, which also drops headers and rules."""
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines() if line.startswith("|")
    ]


def anchors(text: str) -> set:
    """The link targets GitHub makes from a document's headings."""
    slugs = set()
    for title in re.findall(r"^#+ +(.+?)\s*$", text, re.M):
        slugs.add(re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-"))
    return slugs


def template_parameters() -> Dict[str, Dict[str, str]]:
    """{name: {property: text}} for the template's Parameters section."""
    body = re.search(r"^Parameters:\n(.*?)^(?=\S)", read("template.yaml"), re.M | re.S).group(1)
    parameters: Dict[str, Dict[str, str]] = {}
    for line in body.splitlines():
        name = re.match(r"  (\w+):\s*$", line)
        if name:
            parameters[name.group(1)] = {}
        prop = re.match(r"    (\w+):\s*(.*)$", line)
        if prop and parameters:
            parameters[list(parameters)[-1]][prop.group(1)] = prop.group(2).strip().strip("'\"")
    return parameters


def allowed(properties: Dict[str, str]) -> str:
    """How the Settings table words a parameter's limits, or '' when the
    template sets none and the docs may describe the value freely."""
    if "AllowedValues" in properties:
        values = [value.strip(" '\"") for value in properties["AllowedValues"].strip("[]").split(",")]
        return " or ".join("`%s`" % value for value in values)
    low, high = properties.get("MinValue"), properties.get("MaxValue")
    if low is not None and high is not None:
        return "%s to %s" % (low, high)
    return "%s or more" % low if low is not None else ""


class ApiTable(unittest.TestCase):
    def setUp(self):
        self.rows = [
            (re.match(r"`(GET|POST|PUT|PATCH|DELETE) (/\S*)`$", cells[0]), cells)
            for cells in table_rows(read("docs", "api.md"))
        ]
        self.rows = [(match.groups(), cells) for match, cells in self.rows if match]

    def matching(self, method: str, path: str) -> List[int]:
        sample = path.replace("{id}", "AbCdEfGh1234")
        return [i for i, (verb, pattern, _, _) in enumerate(handler.ROUTES)
                if verb == method and pattern.match(sample)]

    def test_every_route_is_documented_once_and_nothing_else_is(self):
        documented = []
        for (method, path), _ in self.rows:
            found = self.matching(method, path)
            self.assertEqual(len(found), 1, "%s %s is in docs/api.md but is not a route" % (method, path))
            documented += found
        self.assertEqual(sorted(documented), list(range(len(handler.ROUTES))),
                         "each route in handler.ROUTES needs exactly one row in docs/api.md")

    def test_the_table_says_who_may_call_each_route(self):
        for (method, path), cells in self.rows:
            who = handler.ROUTES[self.matching(method, path)[0]][2]
            self.assertTrue(cells[1].startswith(WHO[who]),
                            "%s %s: docs/api.md says %r, the code says %r" % (method, path, cells[1], who))


class SettingsTable(unittest.TestCase):
    def setUp(self):
        self.documented = {}
        for cells in table_rows(read("docs", "settings.md")):
            name = re.match(r"`(\w+)`$", cells[0])
            if name:
                self.documented[name.group(1)] = cells
        self.template = template_parameters()

    def test_the_table_lists_exactly_the_templates_parameters(self):
        self.assertGreater(len(self.template), 20)  # the parser found the section
        self.assertEqual(sorted(self.documented), sorted(self.template))

    def test_every_default_matches(self):
        for name, properties in self.template.items():
            default = properties["Default"]
            self.assertEqual(self.documented[name][1], "`%s`" % default if default else "empty", name)

    def test_every_limit_matches(self):
        for name, properties in self.template.items():
            if allowed(properties):
                self.assertEqual(self.documented[name][2], allowed(properties), name)


class Secrets(unittest.TestCase):
    def test_every_secret_the_function_reads_is_documented(self):
        documented = read("docs", "deploy.md")
        for name in ssm_secrets.NAMES:
            self.assertIn("`/emergency-ring/%s`" % name, documented)


class Links(unittest.TestCase):
    def test_relative_links_lead_to_a_file_and_a_heading_that_exist(self):
        for path in markdown_files():
            text = read(path)
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                if re.match(r"[a-z]+:", target):
                    continue  # a web address
                file, _, anchor = target.partition("#")
                linked = os.path.normpath(os.path.join(os.path.dirname(path), file)) if file else path
                where = "%s links to %s" % (os.path.relpath(path, ROOT), target)
                self.assertTrue(os.path.exists(linked), where)
                if anchor:
                    self.assertIn(anchor, anchors(read(linked)), where)


class Leaks(unittest.TestCase):
    """Every published file, not only the docs: a real address or id is as
    likely to arrive in a test fixture, a script or the template. A failure
    names the file and not what was found, so that the value does not end up
    in a test log as well."""

    def test_the_patterns_tell_a_real_shape_from_a_placeholder(self):
        # Built here from pieces, so that this file passes its own scan.
        hex32 = "0123456789abcdef" * 2
        self.assertTrue(TWILIO_ACCOUNT_ID.search("TwilioAccountSid=AC" + hex32))
        self.assertTrue(TWILIO_ACCOUNT_ID.search("AC" + hex32.upper()))
        self.assertFalse(TWILIO_ACCOUNT_ID.search("TwilioAccountSid=AC" + "x" * 32))
        self.assertFalse(TWILIO_ACCOUNT_ID.search("AC" + hex32[:31]))
        for prefix in ("AKIA", "ASIA"):
            self.assertTrue(AWS_ACCESS_KEY_ID.search("key " + prefix + "ABCDEFGH23456789"))
            self.assertFalse(AWS_ACCESS_KEY_ID.search(prefix + "abcdefgh23456789"))
        self.assertEqual(FUNCTION_URL_HOST.findall("https://abc123" + ".lambda-url.us-west-2.on.aws/"),
                         ["abc123"])

    def test_the_scan_covers_the_code_and_not_only_the_docs(self):
        files = published_files()
        for path in ("README.md", "template.yaml", "samconfig.example.toml",
                     "tests/support.py", "src/er/handler.py", "scripts/lib.sh"):
            self.assertIn(path, files)

    def test_no_real_function_url_is_published(self):
        for path, text in published_files().items():
            for host in FUNCTION_URL_HOST.findall(text):
                self.assertTrue(host in PLACEHOLDER_HOSTS,
                                "%s names what may be a real Function URL" % path)

    def test_no_twilio_account_id_is_published(self):
        for path, text in published_files().items():
            self.assertFalse(bool(TWILIO_ACCOUNT_ID.search(text)),
                             "%s holds what looks like a Twilio account id" % path)

    def test_no_aws_access_key_is_published(self):
        for path, text in published_files().items():
            self.assertFalse(bool(AWS_ACCESS_KEY_ID.search(text)),
                             "%s holds what looks like an AWS access key id" % path)


if __name__ == "__main__":
    unittest.main()
