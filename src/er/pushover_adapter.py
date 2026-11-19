"""Pushover over plain HTTPS: the on-screen half of the alert.

Endpoints and field names checked against https://pushover.net/api and
https://pushover.net/api/receipts (November 2026):
  POST /1/messages.json                  priority=2 needs retry and expire; returns "receipt"
  POST /1/receipts/<receipt>/cancel.json with "token"
  GET  /1/receipts/<receipt>.json?token=  returns status and acknowledged (1/0);
                                          poll no faster than every 5 seconds
  The acknowledgment callback is a form POST carrying receipt, acknowledged=1,
  acknowledged_at, acknowledged_by and acknowledged_by_device. Pushover
  retries it a minute later unless it gets a 2xx.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Dict, Optional

from .config import PUSHOVER_MAX_EXPIRE, PUSHOVER_MIN_RETRY
from .models import Incident

API = "https://api.pushover.net/1"
log = logging.getLogger("er")


class PushoverNotifier:
    def __init__(
        self,
        app_token: str,
        owner_user_key: str,
        sender_keys: Optional[Dict[str, str]] = None,
        retry: int = PUSHOVER_MIN_RETRY,
        expire: int = 1800,
        timeout: float = 8.0,
        sound: str = "",
        test_sound: str = "",
    ) -> None:
        self.app_token = app_token
        self.owner_user_key = owner_user_key
        self.sender_keys = sender_keys or {}
        # Config already keeps inside Pushover's limits; this covers a
        # notifier built by hand.
        self.retry = max(int(retry), PUSHOVER_MIN_RETRY)
        self.expire = min(int(expire), PUSHOVER_MAX_EXPIRE)
        self.timeout = timeout
        self.sound = sound
        self.test_sound = test_sound

    def _post(self, path: str, fields: Dict[str, str]) -> Dict:
        request = urllib.request.Request(
            "%s/%s" % (API, path),
            data=urllib.parse.urlencode(fields).encode(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    def send_emergency(self, incident: Incident) -> Optional[str]:
        message = incident.note or "Emergency alert"
        fields = {
            "token": self.app_token,
            "user": self.owner_user_key,
            "title": "%s from %s: %s" % (
                "TEST" if incident.test else "EMERGENCY", incident.sender, incident.kind
            ),
            "message": message,
            "priority": "2",  # emergency: repeats until acknowledged
            "retry": str(self.retry),
            "expire": str(self.expire),
            "callback": "%s/pushover/ack/%s" % (incident.base_url, incident.id),
        }
        quiet = incident.test and not incident.rehearsal
        sound = (self.test_sound or self.sound) if quiet else self.sound
        if sound:
            fields["sound"] = sound
        if incident.lat and incident.lon:
            fields["url"] = "https://maps.apple.com/?ll=%s,%s" % (incident.lat, incident.lon)
            fields["url_title"] = "Open location"
        return self._post("messages.json", fields).get("receipt")

    def cancel_emergency(self, receipt: str) -> None:
        self._post("receipts/%s/cancel.json" % receipt, {"token": self.app_token})

    def emergency_acknowledged(self, receipt: str) -> bool:
        """Ask Pushover itself whether the alert was acknowledged.

        GET /1/receipts/<receipt>.json?token=<app token> returns
        "acknowledged": 1 once the user has acknowledged it.
        """
        url = "%s/receipts/%s.json?%s" % (
            API,
            urllib.parse.quote(receipt, safe=""),
            urllib.parse.urlencode({"token": self.app_token}),
        )
        request = urllib.request.Request(url, method="GET")
        # Runs on every tick ahead of the redial, so it gets a shorter leash.
        with urllib.request.urlopen(request, timeout=min(self.timeout, 4.0)) as response:
            result = json.loads(response.read().decode())
        return result.get("status") == 1 and result.get("acknowledged") == 1

    def send_notice(
        self, title: str, message: str, sound: str = "", url: Optional[str] = None
    ) -> None:
        # Priority 1 (high) is a single alert with no repeat or receipt; the
        # Pushover app plays it as a Critical Alert if "Critical Alerts for
        # high-priority" is on.
        fields = {
            "token": self.app_token,
            "user": self.owner_user_key,
            "title": title,
            "message": message,
            "priority": "1",
        }
        if sound:
            fields["sound"] = sound
        if url:
            fields["url"] = url
        self._post("messages.json", fields)

    def notify_sender(self, sender: str, text: str) -> None:
        user_key = self.sender_keys.get(sender)
        if not user_key:
            log.info("no Pushover key for sender %s; skipping receipt", sender)
            return
        self._post(
            "messages.json",
            {"token": self.app_token, "user": user_key, "title": "Emergency Ring", "message": text},
        )


class NullNotifier:
    """Used when Pushover is not configured: calls only."""

    def send_emergency(self, incident: Incident) -> Optional[str]:
        return None

    def cancel_emergency(self, receipt: str) -> None:
        return None

    def emergency_acknowledged(self, receipt: str) -> bool:
        return False

    def send_notice(
        self, title: str, message: str, sound: str = "", url: Optional[str] = None
    ) -> None:
        return None

    def notify_sender(self, sender: str, text: str) -> None:
        return None
