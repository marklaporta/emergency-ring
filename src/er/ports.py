"""Interfaces the core logic depends on.

Real implementations live in the *_adapter modules; in-memory ones for tests
and the local simulation live in fakes.py.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, Tuple

from .models import Incident


class Store(Protocol):
    def create_if_none_active(self, incident: Incident, now: int) -> Tuple[Incident, bool]:
        """Atomically create the incident unless another one is active.

        Returns (incident, True) when created, (existing_active, False) otherwise.
        """

    def get(self, incident_id: str) -> Optional[Incident]: ...

    def transition(self, incident_id: str, from_status: str, to_status: str, **fields: Any) -> bool:
        """Atomic status change. False if the incident was not in from_status."""

    def advance_tick(self, incident_id: str, expected_seq: int, now: int) -> bool:
        """Atomically bump tick_seq from expected_seq and note the time.
        False on a stale tick."""

    def claim_attempt(self, incident_id: str, expected_attempts: int, now: int) -> bool:
        """Atomically bump attempts and mark a call in flight. False if lost the race."""

    def update(self, incident_id: str, **fields: Any) -> None:
        """Field update on an existing incident. A value of None removes the
        field. Does nothing if the record is gone."""

    def active_id(self) -> Optional[str]:
        """The id the active-incident pointer holds, if any."""

    def set_pause(self, until: int, by: str = "") -> None:
        """Pause all alerting until this time; 0 lifts the pause."""

    def pause_state(self) -> Tuple[int, str]:
        """(when the pause ends, who set it); (0, "") when alerting is on."""

    def claim_slot(
        self, key: str, now: int, interval: int, daily_cap: int
    ) -> Tuple[bool, int, str]:
        """Atomically take the next alert slot for key, if the minimum interval
        and the rolling 24-hour cap both allow it.

        Returns (taken, seconds to wait, reason); reason is "interval" or
        "daily_cap" when refused.
        """

    def peek_slot(self, key: str, now: int, daily_cap: int) -> Tuple[int, str, int]:
        """What claim_slot would say, without taking anything: (seconds to
        wait, reason, alerts sent in the last 24 hours)."""

    def release_slot(self, key: str, at: int) -> None:
        """Give back the slot taken at `at`, because the alert failed to send."""

    def idem_begin(self, key: str, now: int) -> Optional[dict]:
        """Start work under an idempotency key. None means go ahead. Otherwise
        {"state": "pending"} or {"state": "done", "result": {...}}."""

    def idem_finish(self, key: str, result: Optional[dict]) -> None:
        """Record the outcome for the key, or forget the key when result is None."""


class Telephony(Protocol):
    def place_call(self, to: str, incident_id: str, base_url: str, ring_timeout: int) -> str:
        """Start an outbound call. Returns the provider's call id."""

    def hangup(self, call_sid: str) -> None: ...


class Notifier(Protocol):
    def send_emergency(self, incident: Incident) -> Optional[str]:
        """Send the critical alert to the owner. Returns a receipt id if any."""

    def cancel_emergency(self, receipt: str) -> None: ...

    def emergency_acknowledged(self, receipt: str) -> bool:
        """Ask the push provider directly whether the alert was acknowledged."""

    def notify_sender(self, sender: str, text: str) -> None: ...

    def send_notice(
        self, title: str, message: str, sound: str = "", url: Optional[str] = None
    ) -> None:
        """One audible, non-repeating push to the owner."""


class Scheduler(Protocol):
    def schedule_tick(self, incident_id: str, seq: int, delay_seconds: int) -> None: ...

    def schedule_push(self, incident_id: str, delay_seconds: int) -> None:
        """Deliver the first push later, for when the call arm goes first."""


class Clock(Protocol):
    def now(self) -> int: ...
