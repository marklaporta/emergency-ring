from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Optional

ACTIVE = "active"
ACKNOWLEDGED = "acknowledged"
CANCELLED = "cancelled"
EXPIRED = "expired"

_INT_FIELDS = {
    "created_at", "attempts", "calls_failed", "tick_seq", "tick_at", "call_in_flight_since",
    "acked_at", "closed_at",
}


@dataclass
class Incident:
    id: str
    sender: str
    kind: str
    created_at: int
    base_url: str                 # public URL Twilio calls back to
    note: str = ""
    lat: Optional[str] = None     # kept as strings; never do maths on them
    lon: Optional[str] = None
    status: str = ACTIVE
    attempts: int = 0             # calls tried so far
    calls_failed: int = 0         # of those, how many could not be placed
    tick_seq: int = 0             # dedups duplicate tick deliveries
    tick_at: Optional[int] = None  # when the last tick ran: shows the loop is alive
    call_in_flight_since: Optional[int] = None
    last_call_sid: Optional[str] = None
    pushover_receipt: Optional[str] = None
    acked_via: Optional[str] = None
    test: bool = False            # labelled as a test on both arms
    rehearsal: bool = False       # a test that keeps the real alert sound
    acked_at: Optional[int] = None
    closed_at: Optional[int] = None
    cancel_reason: Optional[str] = None  # set when something other than the sender cancelled it

    @property
    def calls_placed(self) -> int:
        return self.attempts - self.calls_failed

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Incident":
        names = {f.name for f in fields(cls)}
        clean: Dict[str, Any] = {}
        for key, value in data.items():
            if key not in names or value is None:
                continue
            # DynamoDB hands numbers back as Decimal.
            clean[key] = int(value) if key in _INT_FIELDS else value
        return cls(**clean)

    def public_view(self) -> Dict[str, Any]:
        """What the sender is allowed to see when polling status."""
        return {
            "id": self.id,
            "status": self.status,
            "kind": self.kind,
            "attempts": self.attempts,
            "calls_failed": self.calls_failed,
            "created_at": self.created_at,
            "acked_via": self.acked_via,
            "test": self.test,
            "rehearsal": self.rehearsal,
            "acked_at": self.acked_at,
        }
