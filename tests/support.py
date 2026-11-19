"""Shared by the test files: puts src/ on the import path, and plays the part
of the Lambda Function URL in front of a service built on fakes.

Import this before anything from `er`. Run the suite the documented way,
`python3 -m unittest discover -s tests`, so that this module can be found.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from er import handler  # noqa: E402
from er.config import Config  # noqa: E402
from er.fakes import NO_ANSWER, Simulation  # noqa: E402

DOMAIN = "abc123.lambda-url.us-west-2.on.aws"
BASE = "https://" + DOMAIN
PRIMARY = "+15550000001"
BACKUP = "+15550000002"
# Long enough that Config does not warn about a guessable token.
ROBIN = "robin-test-token-0123456789"
ASSISTANT = "assistant-test-token-0123456789"
TWILIO_TOKEN = "test-twilio-token"


def make(**overrides):
    """A simulation nobody answers, with Robin as the sender and Assistant as
    the agent, put behind the handler."""
    settings = dict(
        primary_number=PRIMARY,
        trigger_tokens={"Robin": ROBIN},
        break_glass_tokens={"Assistant": ASSISTANT},
        twilio_auth_token=TWILIO_TOKEN,
    )
    settings.update(overrides)
    sim = Simulation(Config(**settings), lambda n, to: NO_ANSWER)
    handler.install(sim.service)
    return sim


def event(method, path, body=None, token=None, headers=None, raw=None, encoded=False):
    """The event a Function URL delivers. `body` is sent as JSON; `raw` is
    sent as it is, for form posts and junk. No token means no header."""
    headers = dict(headers or {})
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if raw is None:
        raw = "" if body is None else json.dumps(body)
    return {
        "rawPath": path, "rawQueryString": "", "isBase64Encoded": encoded,
        "headers": headers, "body": raw,
        "requestContext": {"domainName": DOMAIN, "http": {"method": method}},
    }


def request(method, path, **details):
    """One request through the handler; returns the response as Lambda would."""
    return handler.lambda_handler(event(method, path, **details))


def call(method, path, body=None, token=ROBIN, **details):
    """One request, from Robin unless told otherwise. Returns (status, body),
    the body parsed when it is JSON."""
    response = request(method, path, body=body, token=token, **details)
    try:
        return response["statusCode"], json.loads(response["body"])
    except ValueError:
        return response["statusCode"], response["body"]
