import json
import unittest
import urllib.parse
from unittest import mock

import support  # noqa: F401  (puts src/ on the import path)

from er.models import Incident
from er.pushover_adapter import PushoverNotifier


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class PushoverRequests(unittest.TestCase):
    """The URLs and field names are the ones in Pushover's API documentation."""

    def setUp(self):
        self.requests = []
        self.reply = {"status": 1, "request": "req-1"}

        def urlopen(request, timeout=None):
            fields = urllib.parse.parse_qs(request.data.decode() if request.data else "")
            self.requests.append(
                (request.get_method(), request.full_url, {k: v[0] for k, v in fields.items()})
            )
            return FakeResponse(self.reply)

        patcher = mock.patch("urllib.request.urlopen", urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.notifier = PushoverNotifier("app-token", "owner-key", {"Robin": "robin-key"})
        self.incident = Incident(
            id="abcdefgh1234", sender="Robin", kind="kids", created_at=1,
            base_url="https://example.invalid", note="Call me now.",
            lat="47.610000", lon="-122.330000",
        )

    def test_emergency_message(self):
        self.reply = {"status": 1, "request": "req-1", "receipt": "r" * 30}
        receipt = self.notifier.send_emergency(self.incident)
        self.assertEqual(receipt, "r" * 30)
        method, url, fields = self.requests[0]
        self.assertEqual((method, url), ("POST", "https://api.pushover.net/1/messages.json"))
        self.assertEqual(fields["token"], "app-token")
        self.assertEqual(fields["user"], "owner-key")
        self.assertEqual(fields["priority"], "2")
        self.assertEqual(fields["retry"], "30")
        self.assertEqual(fields["expire"], "1800")
        self.assertEqual(fields["callback"], "https://example.invalid/pushover/ack/abcdefgh1234")
        self.assertEqual(fields["message"], "Call me now.")
        self.assertIn("47.610000,-122.330000", fields["url"])

    def test_retry_and_expire_are_kept_inside_pushover_limits(self):
        notifier = PushoverNotifier("app-token", "owner-key", retry=5, expire=86400)
        notifier.send_emergency(self.incident)
        fields = self.requests[0][2]
        self.assertEqual(fields["retry"], "30")
        self.assertEqual(fields["expire"], "10800")

    def test_cancel_receipt(self):
        self.notifier.cancel_emergency("rcpt123")
        self.assertEqual(
            self.requests[0],
            ("POST", "https://api.pushover.net/1/receipts/rcpt123/cancel.json", {"token": "app-token"}),
        )

    def test_sound_is_sent_only_when_set(self):
        self.notifier.send_emergency(self.incident)
        self.assertNotIn("sound", self.requests[0][2])
        loud = PushoverNotifier("app-token", "owner-key", sound="persistent", retry=90)
        loud.send_emergency(self.incident)
        self.assertEqual(self.requests[1][2]["sound"], "persistent")
        self.assertEqual(self.requests[1][2]["retry"], "90")

    def test_drill_uses_the_test_sound_and_says_test(self):
        notifier = PushoverNotifier("app-token", "owner-key", sound="alien", test_sound="vibrate")
        notifier.send_emergency(self.incident)
        self.incident.test = True
        notifier.send_emergency(self.incident)
        real, drill = self.requests[0][2], self.requests[1][2]
        self.assertEqual((real["sound"], drill["sound"]), ("alien", "vibrate"))
        self.assertTrue(real["title"].startswith("EMERGENCY from Robin"))
        self.assertTrue(drill["title"].startswith("TEST from Robin"))
        # Still emergency priority: a drill exercises the real acknowledgment path.
        self.assertEqual(drill["priority"], "2")
        # A rehearsal is labelled as a test but keeps the real sound.
        self.incident.rehearsal = True
        notifier.send_emergency(self.incident)
        rehearsal = self.requests[2][2]
        self.assertEqual(rehearsal["sound"], "alien")
        self.assertTrue(rehearsal["title"].startswith("TEST from Robin"))

    def test_receipt_lookup(self):
        self.reply = {"status": 1, "acknowledged": 1}
        self.assertTrue(self.notifier.emergency_acknowledged("rcpt123"))
        self.assertEqual(
            self.requests[0],
            ("GET", "https://api.pushover.net/1/receipts/rcpt123.json?token=app-token", {}),
        )
        self.reply = {"status": 1, "acknowledged": 0}
        self.assertFalse(self.notifier.emergency_acknowledged("rcpt123"))
        self.reply = {"status": 0, "acknowledged": 1}
        self.assertFalse(self.notifier.emergency_acknowledged("rcpt123"))

    def test_sender_receipt_is_normal_priority_and_skipped_without_a_key(self):
        self.notifier.notify_sender("Robin", "The owner acknowledged your alert.")
        self.notifier.notify_sender("Nobody", "ignored")
        self.assertEqual(len(self.requests), 1)
        fields = self.requests[0][2]
        self.assertEqual(fields["user"], "robin-key")
        self.assertNotIn("priority", fields)

    def test_notice_is_high_priority_once_with_no_receipt(self):
        self.notifier.send_notice("Assistant: Voicemail", "Call the school.", sound="magic",
                                  url="https://example.invalid/vm")
        fields = self.requests[0][2]
        self.assertEqual((fields["priority"], fields["sound"]), ("1", "magic"))
        self.assertEqual(fields["url"], "https://example.invalid/vm")
        # No repeat and nothing to acknowledge.
        for absent in ("retry", "expire", "callback"):
            self.assertNotIn(absent, fields)


if __name__ == "__main__":
    unittest.main()
