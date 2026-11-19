"""Twilio voice over plain HTTPS. No SDK, so the Lambda has no dependencies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
import urllib.request
from typing import Dict

API = "https://api.twilio.com/2010-04-01"


def compute_signature(auth_token: str, url: str, params: Dict[str, str]) -> str:
    """Twilio's webhook signature: HMAC-SHA1 over the full URL followed by
    every POST field as key+value, sorted by key."""
    payload = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def signature_is_valid(auth_token: str, url: str, params: Dict[str, str], signature: str) -> bool:
    expected = compute_signature(auth_token, url, params)
    # As bytes: the header comes from anyone, and comparing text that is not
    # ASCII raises.
    return hmac.compare_digest(expected.encode(), (signature or "").encode())


class TwilioTelephony:
    def __init__(self, account_sid: str, auth_token: str, from_number: str, timeout: float = 8.0) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.timeout = timeout

    def _post(self, path: str, fields: Dict[str, str]) -> Dict:
        request = urllib.request.Request(
            "%s/Accounts/%s/%s" % (API, self.account_sid, path),
            data=urllib.parse.urlencode(fields).encode(),
            method="POST",
        )
        basic = base64.b64encode(("%s:%s" % (self.account_sid, self.auth_token)).encode()).decode()
        request.add_header("Authorization", "Basic " + basic)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    def place_call(self, to: str, incident_id: str, base_url: str, ring_timeout: int) -> str:
        result = self._post(
            "Calls.json",
            {
                "To": to,
                "From": self.from_number,
                "Url": "%s/twilio/voice/%s" % (base_url, incident_id),
                "Method": "POST",
                "StatusCallback": "%s/twilio/status/%s" % (base_url, incident_id),
                "StatusCallbackMethod": "POST",
                "Timeout": str(ring_timeout),
            },
        )
        return result["sid"]

    def hangup(self, call_sid: str) -> None:
        self._post("Calls/%s.json" % call_sid, {"Status": "completed"})


class NullTelephony:
    """Used when the call arm is switched off. Nothing should ever dial."""

    def place_call(self, to: str, incident_id: str, base_url: str, ring_timeout: int) -> str:
        raise RuntimeError("the call arm is disabled")

    def hangup(self, call_sid: str) -> None:
        return None
