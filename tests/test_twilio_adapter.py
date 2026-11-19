"""The Twilio adapter: the requests it sends and the signature it checks.

Nothing here reaches Twilio. urllib.request.urlopen is replaced, so each test
sees the exact request that would have gone out. The signature vectors are
written out by hand from Twilio's documented rule, so they do not depend on
the code they check.
"""
import base64
import hashlib
import hmac
import io
import itertools
import json
import os
import socket
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import support
from support import PRIMARY

from er import handler
from er.config import Config
from er.fakes import NO_ANSWER, Simulation
from er.twilio_adapter import TwilioTelephony, compute_signature, signature_is_valid

# Placeholders in the shape of the real things; none of them exists.
ACCOUNT = "ACtest00000000000000000000000000"
TOKEN = "adapter-auth-token-0123456789"
FROM = "+15550000009"
BASE_URL = "https://ring.example.invalid"


class Sent:
    """One request as urlopen received it."""

    def __init__(self, request, timeout):
        self.method = request.get_method()
        self.url = request.full_url
        self.body = request.data.decode() if request.data else ""
        self.fields = dict(urllib.parse.parse_qsl(self.body, keep_blank_values=True))
        self.authorization = request.get_header("Authorization")
        self.timeout = timeout


class _Reply:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class RecordedHttp:
    """Stands in for urllib.request.urlopen for the length of one test. It
    keeps every request and answers with `reply`; a reply that is an
    exception is raised instead, the way urlopen reports a failure."""

    def __init__(self, testcase, reply=None):
        self.requests = []
        self.reply = {} if reply is None else reply
        patcher = mock.patch("urllib.request.urlopen", self)
        patcher.start()
        testcase.addCleanup(patcher.stop)

    def __call__(self, request, timeout=None):
        self.requests.append(Sent(request, timeout))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return _Reply(self.reply)


def http_error(code):
    """What urlopen raises for a reply outside 2xx."""
    return urllib.error.HTTPError(
        "https://api.twilio.com/", code, "refused", {}, io.BytesIO(b'{"message": "refused"}')
    )


class Requests(unittest.TestCase):
    """URLs and field names are those of Twilio's Calls resource."""

    def setUp(self):
        self.http = RecordedHttp(self, {"sid": "CA123", "status": "queued"})
        self.telephony = TwilioTelephony(ACCOUNT, TOKEN, FROM)

    def place(self, ring_timeout=25):
        return self.telephony.place_call(PRIMARY, "incident-aaa", BASE_URL, ring_timeout)

    def test_place_call_sends_one_post_with_every_field(self):
        self.assertEqual(self.place(), "CA123")
        (sent,) = self.http.requests
        self.assertEqual(
            (sent.method, sent.url),
            ("POST", "https://api.twilio.com/2010-04-01/Accounts/%s/Calls.json" % ACCOUNT),
        )
        self.assertEqual(
            sent.fields,
            {
                "To": PRIMARY,
                "From": FROM,
                "Url": BASE_URL + "/twilio/voice/incident-aaa",
                "Method": "POST",
                "StatusCallback": BASE_URL + "/twilio/status/incident-aaa",
                "StatusCallbackMethod": "POST",
                "Timeout": "25",
            },
        )

    def test_the_plus_of_a_phone_number_survives_form_encoding(self):
        # Sent bare, a "+" reads as a space and Twilio would dial nothing.
        self.place()
        self.assertIn("To=%2B", self.http.requests[0].body)
        self.assertIn("From=%2B", self.http.requests[0].body)

    def test_the_ring_timeout_is_the_one_asked_for(self):
        # This is what ends the ring before voicemail picks up.
        self.place(ring_timeout=40)
        self.assertEqual(self.http.requests[0].fields["Timeout"], "40")

    def test_hangup_completes_that_call_and_nothing_else(self):
        self.assertIsNone(self.telephony.hangup("CA9"))
        (sent,) = self.http.requests
        self.assertEqual(
            (sent.method, sent.url),
            ("POST", "https://api.twilio.com/2010-04-01/Accounts/%s/Calls/CA9.json" % ACCOUNT),
        )
        self.assertEqual(sent.fields, {"Status": "completed"})

    def test_every_request_carries_basic_auth_for_the_account(self):
        self.place()
        self.telephony.hangup("CA9")
        expected = ("%s:%s" % (ACCOUNT, TOKEN)).encode()
        for sent in self.http.requests:
            scheme, _, encoded = sent.authorization.partition(" ")
            self.assertEqual(scheme, "Basic")
            # A yes or no, so a failure never prints the header.
            self.assertTrue(
                hmac.compare_digest(base64.b64decode(encoded), expected),
                "the Authorization header is not base64 of 'account sid:auth token'",
            )
            # The token belongs in that header and nowhere else.
            self.assertNotIn(TOKEN, sent.url)
            self.assertNotIn(TOKEN, sent.body)

    def test_requests_give_up_after_the_timeout(self):
        # Without one, urlopen waits for as long as the Lambda is allowed to run.
        self.place()
        self.assertEqual(self.http.requests[0].timeout, 8.0)
        TwilioTelephony(ACCOUNT, TOKEN, FROM, timeout=3.0).hangup("CA9")
        self.assertEqual(self.http.requests[1].timeout, 3.0)

    # Core counts a call as failed only when place_call raises, so every way
    # Twilio can say no has to come out as an exception.

    def test_a_refusal_from_twilio_raises(self):
        for code in (400, 401, 429, 500, 503):
            self.http.reply = http_error(code)
            with self.assertRaises(urllib.error.HTTPError, msg=code):
                self.place()
            with self.assertRaises(urllib.error.HTTPError, msg=code):
                self.telephony.hangup("CA9")

    def test_a_timeout_raises(self):
        self.http.reply = socket.timeout("timed out")
        with self.assertRaises(socket.timeout):
            self.place()
        with self.assertRaises(socket.timeout):
            self.telephony.hangup("CA9")

    def test_an_unreachable_host_raises(self):
        self.http.reply = urllib.error.URLError("no route to host")
        with self.assertRaises(urllib.error.URLError):
            self.place()

    def test_a_reply_without_a_call_id_raises(self):
        # Better a failed attempt than a call recorded under the id None,
        # which no status callback could ever match.
        self.http.reply = {"status": "queued"}
        with self.assertRaises(KeyError):
            self.place()


class ThroughCore(unittest.TestCase):
    """The real adapter behind the real state machine, calls first."""

    def setUp(self):
        self.http = RecordedHttp(self, {"sid": "CA123"})
        self.sim = Simulation(
            Config(primary_number=PRIMARY, first_arm="call", ring_timeout=17),
            lambda n, to: NO_ANSWER,
        )
        self.sim.service.telephony = TwilioTelephony(ACCOUNT, TOKEN, FROM)

    def test_the_configured_ring_timeout_and_the_callback_address_reach_twilio(self):
        started = self.sim.trigger()
        (sent,) = self.http.requests
        self.assertEqual(sent.fields["To"], PRIMARY)
        self.assertEqual(sent.fields["Timeout"], "17")
        self.assertEqual(sent.fields["Url"], "%s/twilio/voice/%s" % (started.base_url, started.id))
        self.assertEqual(self.sim.store.get(started.id).last_call_sid, "CA123")

    def test_a_refusal_from_twilio_is_counted_as_a_call_that_failed(self):
        self.http.reply = http_error(401)
        with self.assertLogs("er", level="ERROR"):
            started = self.sim.trigger()
        record = self.sim.store.get(started.id)
        self.assertEqual((record.attempts, record.calls_failed), (1, 1))
        self.assertIsNone(record.last_call_sid)
        self.assertIsNone(record.call_in_flight_since)


# One request written out in full. The fields are deliberately out of order,
# one is empty, and "CallSid" and "Called" sort differently depending on
# whether case counts.
VECTOR_TOKEN = "vector-auth-token-0123456789"
VECTOR_URL = "https://ring.example.invalid/twilio/status/incident-aaa?attempt=2"
VECTOR_FIELDS = {
    "To": "+15550000001",
    "Called": "+15550000001",
    "CallStatus": "completed",
    "From": "+15550000009",
    "ForwardedFrom": "",
    "Caller": "+15550000009",
    "Digits": "1",
    "CallSid": "CA00000000000000000000000000000001",
}
# Twilio's rule: the full URL, query string included, then every POST field as
# name followed by value with nothing in between, ordered by name the way Unix
# sorts (capitals before small letters).
VECTOR_SIGNED_TEXT = (
    VECTOR_URL
    + "CallSid" + "CA00000000000000000000000000000001"
    + "CallStatus" + "completed"
    + "Called" + "+15550000001"
    + "Caller" + "+15550000009"
    + "Digits" + "1"
    + "ForwardedFrom" + ""
    + "From" + "+15550000009"
    + "To" + "+15550000001"
)
# base64(HMAC-SHA1(token, signed text)), worked out once and pasted here.
VECTOR_SIGNATURE = "idlzJCKdw2FUFmL1LpkE+vgbddo="


class Signature(unittest.TestCase):
    def test_the_vector_is_what_the_documented_rule_gives(self):
        digest = hmac.new(VECTOR_TOKEN.encode(), VECTOR_SIGNED_TEXT.encode(), hashlib.sha1).digest()
        self.assertEqual(base64.b64encode(digest).decode(), VECTOR_SIGNATURE)

    def test_known_vector(self):
        self.assertEqual(
            compute_signature(VECTOR_TOKEN, VECTOR_URL, VECTOR_FIELDS), VECTOR_SIGNATURE
        )
        self.assertTrue(
            signature_is_valid(VECTOR_TOKEN, VECTOR_URL, VECTOR_FIELDS, VECTOR_SIGNATURE)
        )

    def test_twilio_documentation_example_with_the_fields_out_of_order(self):
        fields = {
            "To": "+18005551212",
            "Digits": "1234",
            "From": "+12349013030",
            "Caller": "+12349013030",
            "CallSid": "CA1234567890ABCDE",
        }
        self.assertEqual(
            compute_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", fields),
            "0/KCTR6DLpKmkAf8muzZqo1nDgQ=",
        )

    def test_the_order_fields_arrive_in_does_not_matter(self):
        # A form body has no promised order, and a dict keeps whatever it got.
        names = ["To", "Called", "CallSid", "Digits"]
        for order in itertools.permutations(names):
            fields = {name: VECTOR_FIELDS[name] for name in order}
            self.assertEqual(
                compute_signature(VECTOR_TOKEN, VECTOR_URL, fields),
                compute_signature(VECTOR_TOKEN, VECTOR_URL, {n: VECTOR_FIELDS[n] for n in names}),
                order,
            )

    def test_the_query_string_is_signed(self):
        without_query = VECTOR_URL.split("?")[0]
        for url in (without_query, without_query + "?attempt=3", VECTOR_URL + "&x=1"):
            self.assertFalse(
                signature_is_valid(VECTOR_TOKEN, url, VECTOR_FIELDS, VECTOR_SIGNATURE), url
            )

    def test_any_change_to_the_request_breaks_the_signature(self):
        def valid(token=VECTOR_TOKEN, url=VECTOR_URL, **changed):
            fields = dict(VECTOR_FIELDS, **changed)
            return signature_is_valid(token, url, fields, VECTOR_SIGNATURE)

        self.assertTrue(valid())
        self.assertFalse(valid(Digits="2"))                 # a value changed
        self.assertFalse(valid(Extra="1"))                  # a field added
        self.assertFalse(valid(token=VECTOR_TOKEN + "x"))   # signed with another token
        self.assertFalse(valid(url=VECTOR_URL.replace("incident-aaa", "incident-bbb")))
        without_digits = {k: v for k, v in VECTOR_FIELDS.items() if k != "Digits"}
        self.assertFalse(
            signature_is_valid(VECTOR_TOKEN, VECTOR_URL, without_digits, VECTOR_SIGNATURE)
        )

    def test_a_missing_or_odd_signature_is_refused_without_an_error(self):
        # The header comes from anyone on the internet.
        for signature in ("", None, VECTOR_SIGNATURE + " ", VECTOR_SIGNATURE.lower(), "café"):
            self.assertFalse(
                signature_is_valid(VECTOR_TOKEN, VECTOR_URL, VECTOR_FIELDS, signature),
                repr(signature),
            )

    def test_the_handler_accepts_the_vector_and_nothing_near_it(self):
        # The same request through the front door, signed with the literal
        # above. This is the only test where the handler has to rebuild the
        # signed URL and form by itself: query string, empty field and all.
        support.make(twilio_auth_token=VECTOR_TOKEN)
        address = urllib.parse.urlsplit(VECTOR_URL)

        def post(signature, query=address.query, fields=VECTOR_FIELDS):
            event = support.event(
                "POST", address.path, raw=urllib.parse.urlencode(fields),
                headers={"X-Twilio-Signature": signature},
            )
            event["requestContext"]["domainName"] = address.netloc
            event["rawQueryString"] = query
            return handler.lambda_handler(event)["statusCode"]

        # A deployment may pin its public address; this request has its own.
        with mock.patch.dict(os.environ):
            os.environ.pop("PUBLIC_BASE_URL", None)
            self.assertEqual(post(VECTOR_SIGNATURE), 200)
            with self.assertLogs("er", level="WARNING"):  # a refusal leaves a trace
                self.assertEqual(post(VECTOR_SIGNATURE, query=""), 403)
                self.assertEqual(
                    post(VECTOR_SIGNATURE, fields=dict(VECTOR_FIELDS, Digits="2")), 403
                )
                self.assertEqual(post(""), 403)


if __name__ == "__main__":
    unittest.main()
