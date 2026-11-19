"""In-memory stand-ins for every external dependency, plus a virtual-time
simulation that plays the part of Twilio and the person answering."""
from __future__ import annotations

import heapq
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config
from .core import Service
from . import limits
from .models import ACTIVE, Incident


class FakeClock:
    def __init__(self, start: int = 1_000_000) -> None:
        self.t = start

    def now(self) -> int:
        return self.t


class InMemoryStore:
    def __init__(self, pointer_ttl: int = 2 * 3600) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.active_id_: Optional[str] = None
        self.active_expires = 0
        self.pointer_ttl = pointer_ttl
        self.cooldowns: Dict[str, Dict[str, Any]] = {}
        self.idem: Dict[str, Dict[str, Any]] = {}
        self.pause: Tuple[int, str] = (0, "")

    def active_id(self) -> Optional[str]:
        return self.active_id_

    def set_pause(self, until: int, by: str = "") -> None:
        self.pause = (until, by) if until > 0 else (0, "")

    def pause_state(self) -> Tuple[int, str]:
        return self.pause

    def create_if_none_active(self, incident: Incident, now: int) -> Tuple[Incident, bool]:
        held = self.items.get(self.active_id_ or "")
        # As in the real store, a pointer past its lifetime blocks nothing.
        if held and held["status"] == ACTIVE and now <= self.active_expires:
            return Incident.from_dict(held), False
        self.items[incident.id] = incident.to_dict()
        self.active_id_ = incident.id
        self.active_expires = now + self.pointer_ttl
        return incident, True

    def get(self, incident_id: str) -> Optional[Incident]:
        item = self.items.get(incident_id)
        return Incident.from_dict(item) if item else None

    def transition(self, incident_id: str, from_status: str, to_status: str, **fields: Any) -> bool:
        item = self.items.get(incident_id)
        if not item or item["status"] != from_status:
            return False
        item["status"] = to_status
        item.update(fields)
        if to_status != ACTIVE and self.active_id_ == incident_id:
            self.active_id_ = None
        return True

    def advance_tick(self, incident_id: str, expected_seq: int, now: int) -> bool:
        item = self.items.get(incident_id)
        if not item or item["status"] != ACTIVE or item.get("tick_seq", 0) != expected_seq:
            return False
        item["tick_seq"], item["tick_at"] = expected_seq + 1, now
        return True

    def claim_attempt(self, incident_id: str, expected_attempts: int, now: int) -> bool:
        item = self.items.get(incident_id)
        if not item or item["status"] != ACTIVE or item.get("attempts", 0) != expected_attempts:
            return False
        item["attempts"] = expected_attempts + 1
        item["call_in_flight_since"] = now
        return True

    def claim_slot(self, key: str, now: int, interval: int, daily_cap: int) -> Tuple[bool, int, str]:
        slot = self.cooldowns.setdefault(key, {"free_at": 0, "sent": []})
        ok, wait, reason, recent = limits.decide(slot["free_at"], slot["sent"], now, daily_cap)
        if ok:
            slot["free_at"], slot["sent"] = now + interval, recent + [now]
        return ok, wait, reason

    def peek_slot(self, key: str, now: int, daily_cap: int) -> Tuple[int, str, int]:
        slot = self.cooldowns.get(key, {"free_at": 0, "sent": []})
        _, wait, reason, recent = limits.decide(slot["free_at"], slot["sent"], now, daily_cap)
        return wait, reason, len(recent)

    def release_slot(self, key: str, at: int) -> None:
        slot = self.cooldowns.get(key)
        if slot and at in slot["sent"]:
            slot["sent"].remove(at)
            slot["free_at"] = 0

    def idem_begin(self, key: str, now: int) -> Optional[dict]:
        prior = self.idem.get(key)
        abandoned = prior is not None and prior.get("began", now) < now - limits.IDEM_PENDING_SECONDS
        if prior is not None and not abandoned:
            return {k: v for k, v in prior.items() if k != "began"}
        self.idem[key] = {"state": "pending", "began": now}
        return None

    def idem_finish(self, key: str, result: Optional[dict]) -> None:
        if result is None:
            self.idem.pop(key, None)
        else:
            self.idem[key] = {"state": "done", "result": dict(result)}

    def update(self, incident_id: str, **fields: Any) -> None:
        item = self.items.get(incident_id)
        if item is None:
            return
        for key, value in fields.items():
            if value is None:
                item.pop(key, None)
            else:
                item[key] = value


class FakeTelephony:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.hung_up: List[str] = []
        self.fail_next = False

    def place_call(self, to: str, incident_id: str, base_url: str, ring_timeout: int) -> str:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("carrier unavailable")
        sid = "CA%04d" % (len(self.calls) + 1)
        self.calls.append({"sid": sid, "to": to, "incident_id": incident_id})
        return sid

    def hangup(self, call_sid: str) -> None:
        self.hung_up.append(call_sid)


class FakeNotifier:
    def __init__(self) -> None:
        self.emergencies: List[str] = []
        self.cancelled: List[str] = []
        self.sender_messages: List[Tuple[str, str]] = []
        self.notices: List[Dict[str, Any]] = []
        # Receipts the owner has acknowledged in the Pushover app.
        self.acknowledged: set = set()
        self.lookups = 0
        self.lookup_fails = False

    def send_emergency(self, incident: Incident) -> Optional[str]:
        receipt = "rcpt-%s" % incident.id
        self.emergencies.append(receipt)
        return receipt

    def cancel_emergency(self, receipt: str) -> None:
        self.cancelled.append(receipt)

    def emergency_acknowledged(self, receipt: str) -> bool:
        self.lookups += 1
        if self.lookup_fails:
            raise RuntimeError("pushover unreachable")
        return receipt in self.acknowledged

    def notify_sender(self, sender: str, text: str) -> None:
        self.sender_messages.append((sender, text))

    def send_notice(
        self, title: str, message: str, sound: str = "", url: Optional[str] = None
    ) -> None:
        self.notices.append({"title": title, "message": message, "sound": sound, "url": url})


class FakeScheduler:
    """Holds ticks in a heap keyed by virtual due time."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.queue: List[Tuple[int, int, str, int]] = []
        self.pushes: List[Tuple[int, int, str]] = []
        self._n = 0

    def schedule_tick(self, incident_id: str, seq: int, delay_seconds: int) -> None:
        self._n += 1
        heapq.heappush(self.queue, (self.clock.now() + delay_seconds, self._n, incident_id, seq))

    def schedule_push(self, incident_id: str, delay_seconds: int) -> None:
        self._n += 1
        heapq.heappush(self.pushes, (self.clock.now() + delay_seconds, self._n, incident_id))


# What the person on the other end does with call number N (1-based).
NO_ANSWER = "no-answer"   # rings out
VOICEMAIL = "voicemail"   # a machine picks up; nobody presses 1
ACK = "ack"               # answers and presses 1
LOST_CALLBACK = "lost"    # rings out and Twilio's status callback never arrives
PUSH_ACK = "push-ack"     # acknowledges the alert in the Pushover app


class Simulation:
    """Runs the real Service against fakes in virtual time."""

    def __init__(self, config: Config, behaviour: Callable[[int, str], str]) -> None:
        self.clock = FakeClock()
        self.start = self.clock.now()
        self.store = InMemoryStore(config.pointer_ttl)
        self.telephony = FakeTelephony()
        self.notifier = FakeNotifier()
        self.scheduler = FakeScheduler(self.clock)
        self.cfg = config
        self.service = Service(
            config, self.store, self.telephony, self.notifier, self.scheduler, self.clock
        )
        self.behaviour = behaviour
        self.log: List[str] = []
        # Call outcomes waiting to happen: (due, n, kind, incident_id, sid)
        self._events: List[Tuple[int, int, str, str, str]] = []
        self._handled_calls = 0
        self._handled_pushes = 0
        self._n = 0
        # Set to N to have the owner acknowledge the push N seconds after the
        # trigger (if a push has reached them by then).
        self.push_ack_after: Optional[int] = None

    def say(self, text: str) -> None:
        elapsed = self.clock.now() - self.start
        self.log.append("t+%3ds  %s" % (elapsed, text))

    def trigger(self, sender: str = "Robin", kind: str = "kids", **kw: Any) -> Incident:
        incident, created = self.service.trigger(
            sender, kind, "https://example.invalid", **kw
        )
        self.say("%s triggers '%s' -> %s" % (sender, kind, "new incident" if created else "attached to active incident"))
        if created and self.push_ack_after is not None:
            self._n += 1
            heapq.heappush(
                self._events,
                (self.clock.now() + self.push_ack_after, self._n, PUSH_ACK, incident.id, ""),
            )
        self._note_new_alerts()
        return incident

    def _note_new_alerts(self) -> None:
        """Log each push that has gone out, and line up what happens to each
        call that was placed."""
        while self._handled_pushes < len(self.notifier.emergencies):
            self._handled_pushes += 1
            self.say("push alert sent (repeats every %ds)" % self.cfg.pushover_retry)
        while self._handled_calls < len(self.telephony.calls):
            call = self.telephony.calls[self._handled_calls]
            self._handled_calls += 1
            n = self._handled_calls
            what = self.behaviour(n, call["to"])
            self.say("call %d placed to %s" % (n, call["to"]))
            delay = 10 if what in (ACK, VOICEMAIL) else self.cfg.ring_timeout
            self._n += 1
            heapq.heappush(
                self._events,
                (self.clock.now() + delay, self._n, what, call["incident_id"], call["sid"]),
            )

    def _play_event(self, what: str, incident_id: str, sid: str) -> None:
        svc = self.service
        if what == PUSH_ACK:
            incident = self.store.get(incident_id)
            if incident is None or not incident.pushover_receipt or incident.status != ACTIVE:
                return
            self.notifier.acknowledged.add(incident.pushover_receipt)
            svc.acknowledge_from_push(incident_id, incident.pushover_receipt)
            self.say("acknowledged in the Pushover app")
            return
        if self.store.get(incident_id).status != ACTIVE and sid in self.telephony.hung_up:
            self.say("call was hung up by the backend")
            return
        if what == ACK:
            twiml = svc.voice_twiml(incident_id)
            spoken = re.search(r"<Say>(.*?)</Say>", twiml)
            self.say('answered; hears "%s"' % (spoken.group(1) if spoken else "?"))
            svc.ack_from_call(incident_id, "1")
            self.say("pressed 1 -> acknowledged")
            svc.call_status(incident_id, sid, "completed")
        elif what == VOICEMAIL:
            svc.voice_twiml(incident_id)
            self.say("voicemail answered; no keypress, so not acknowledged")
            self.clock.t += 30
            svc.call_status(incident_id, sid, "completed")
        elif what == NO_ANSWER:
            self.say("rang out, no answer")
            svc.call_status(incident_id, sid, "no-answer")
        elif what == LOST_CALLBACK:
            self.say("rang out; status callback lost")

    def run(self, max_steps: int = 200) -> None:
        """Advance virtual time until nothing is left to happen."""
        for _ in range(max_steps):
            next_event = self._events[0][0] if self._events else None
            next_tick = self.scheduler.queue[0][0] if self.scheduler.queue else None
            next_push = self.scheduler.pushes[0][0] if self.scheduler.pushes else None
            if next_event is None and next_tick is None and next_push is None:
                return
            soonest = min(t for t in (next_event, next_tick, next_push) if t is not None)
            if next_event == soonest:
                due, _, what, incident_id, sid = heapq.heappop(self._events)
                self.clock.t = max(self.clock.t, due)
                self._play_event(what, incident_id, sid)
            elif next_push == soonest:
                due, _, incident_id = heapq.heappop(self.scheduler.pushes)
                self.clock.t = max(self.clock.t, due)
                self.service.deliver_push(incident_id)
                self._note_new_alerts()
            else:
                due, _, incident_id, seq = heapq.heappop(self.scheduler.queue)
                self.clock.t = max(self.clock.t, due)
                result = self.service.tick(incident_id, seq)
                if result != "dialed":
                    self.say("tick %d -> %s" % (seq, result))
                self._note_new_alerts()
        raise RuntimeError("simulation did not settle")
