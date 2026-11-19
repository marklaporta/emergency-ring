"""Incident state machine. No AWS, Twilio or Pushover imports in here."""
from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any, Callable, List, Optional, Tuple
from xml.sax.saxutils import escape

from .config import Config
from .models import ACKNOWLEDGED, ACTIVE, CANCELLED, EXPIRED, Incident
from .ports import Clock, Notifier, Scheduler, Store, Telephony

log = logging.getLogger("er")

MAX_KIND = 40
MAX_NOTE = 200
MAX_TITLE = 100
MAX_MESSAGE = 700

SELF_TEST_SENDER = "Self-test"
# A self-test rings the phone, so an agent may start one a day and no more.
# Senders are not limited: they can already run a drill through /trigger.
AGENT_SELF_TEST_INTERVAL = 24 * 3600
# The kill switch always runs out by itself, whoever calls it.
MAX_PAUSE_SECONDS = 24 * 3600
# A first push that fails is handed to the queue, which keeps trying.
PUSH_RETRY_SECONDS = 5

# Why an incident was cancelled, when it was not the sender's own choice.
PAUSED = "paused"
SUPERSEDED = "superseded"
FAILED = "failed"
STALLED = "stalled"

Outcome = Tuple[Any, Optional[BaseException]]


def side_by_side(jobs: List[Callable[[], Any]]) -> List[Outcome]:
    """Run slow, independent vendor calls at the same time and wait for all
    of them. Returns (value, error) per job, in order; nothing is raised.

    Waiting matters: Lambda freezes a thread that is still running when the
    handler returns. Store writes stay with the caller, on its own thread.
    """
    outcomes: List[Outcome] = [(None, None)] * len(jobs)

    def run(i: int) -> None:
        try:
            outcomes[i] = (jobs[i](), None)
        except BaseException as error:  # reported to the caller, never lost
            outcomes[i] = (None, error)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(1, len(jobs))]
    for thread in threads:
        thread.start()
    if jobs:
        run(0)
    for thread in threads:
        thread.join()
    return outcomes


class SystemClock:
    def now(self) -> int:
        return int(time.time())


class Paused(Exception):
    """Alerting is paused; nothing was sent."""

    def __init__(self, seconds: int) -> None:
        super().__init__("paused for another %d seconds" % seconds)
        self.seconds = seconds


class Service:
    def __init__(
        self,
        config: Config,
        store: Store,
        telephony: Telephony,
        notifier: Notifier,
        scheduler: Scheduler,
        clock: Optional[Clock] = None,
    ) -> None:
        self.cfg = config
        self.store = store
        self.telephony = telephony
        self.notifier = notifier
        self.scheduler = scheduler
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ trigger

    def trigger(
        self,
        sender: str,
        kind: str,
        base_url: str,
        note: str = "",
        lat: Optional[str] = None,
        lon: Optional[str] = None,
        test: bool = False,
        rehearsal: bool = False,
    ) -> Tuple[Incident, bool]:
        """Start an incident, or return the one already active.

        test=True runs the same flow as a drill: the push uses the quieter
        test sound and both arms say that it is a test. rehearsal=True is a
        test under real conditions: still labelled as a test, but with the
        real alert sound.
        """
        now = self.clock.now()
        paused = self.paused_seconds()
        if paused:
            log.warning("trigger from %s refused: paused for %ds", sender, paused)
            raise Paused(paused)
        requested = Incident(
            id=secrets.token_urlsafe(12),
            sender=sender,
            kind=(kind or "emergency")[:MAX_KIND],
            note=(note or "")[:MAX_NOTE],
            lat=lat,
            lon=lon,
            created_at=now,
            base_url=base_url.rstrip("/"),
            test=bool(test) or bool(rehearsal),
            rehearsal=bool(rehearsal),
        )
        incident, created = self.store.create_if_none_active(requested, now)
        replaced: List[Incident] = []
        # More than once: another test can start in the moment between
        # closing one and creating the real incident.
        for _ in range(3):
            why = "" if created else self._why_replace(incident, requested, now)
            if not why:
                break
            log.warning("trigger from %s replaces incident %s (%s)", sender, incident.id, why)
            if self._close(incident, why):
                replaced.append(incident)
            incident, created = self.store.create_if_none_active(requested, now)
        if not created:
            log.info("trigger attached to active incident %s", incident.id)
            return incident, False
        try:
            return self._start(incident), True
        finally:
            # Afterwards, so the new alert never waits behind taking an old
            # one back.
            for old in replaced:
                self._wind_down(old, hangup=True)

    def _start(self, incident: Incident) -> Incident:
        """Queue the follow-up for a new incident and send what is due now."""
        cfg = self.cfg
        call_now = cfg.call_enabled and cfg.call_delay == 0
        push_now = cfg.push_enabled and cfg.push_delay == 0

        # Everything that happens later is scheduled first: if this fails the
        # caller gets an error and can fall back, instead of an incident that
        # alerts once and is never followed up.
        #
        # The tick loop places the calls. When the first call is delayed, the
        # first tick is that call; otherwise ticks start one cycle from now.
        first_tick = cfg.call_delay if (cfg.call_enabled and not call_now) else cfg.cycle_seconds
        try:
            self.scheduler.schedule_tick(incident.id, incident.tick_seq, first_tick)
            if cfg.push_enabled and not push_now:
                self.scheduler.schedule_push(incident.id, cfg.push_delay)
        except Exception:
            # Nothing would follow this incident up. Left active, it would
            # absorb the sender's next attempt and alert nobody.
            self._abandon(incident)
            raise

        self._fire(incident, call=call_now, push=push_now)
        return self._settle(incident)

    def _settle(self, incident: Incident) -> Incident:
        """Read the incident back after sending. If it closed while its
        alerts were going out (an acknowledgment, a pause, a cancel), whoever
        closed it could not take back what had not been sent yet, so that is
        done here."""
        latest = self.store.get(incident.id) or incident
        if latest.status != ACTIVE:
            self._wind_down(latest, hangup=True)
        return latest

    def _why_replace(self, active: Incident, requested: Incident, now: int) -> str:
        """Why a new trigger should take the place of the active incident
        and not join it; empty when it should join."""
        if active.test and not requested.test:
            # A real alert must never ride along on a drill and go out
            # labelled as a test.
            return SUPERSEDED
        if requested.test and not active.test:
            # A test never takes the place of a real alert, even a stalled
            # one: its push may still be repeating on the owner's phone.
            return ""
        return STALLED if self._stalled(active, now) else ""

    def _stalled(self, incident: Incident, now: int) -> bool:
        """True when nothing is driving an active incident any more, so that
        joining it would tell the sender "still alerting" while the phone
        stays silent."""
        if now - (incident.tick_at or incident.created_at) > self.cfg.abandoned_after:
            return True  # its ticks stopped: parked after repeated failures, or lost
        # Or every arm has had its turn and nothing got out: it died at
        # birth, or its only alert kept failing.
        overdue = now - incident.created_at > max(self.cfg.call_delay, self.cfg.push_delay) + 60
        return overdue and self.nothing_sent(incident)

    def _abandon(self, incident: Incident) -> None:
        try:
            self.store.transition(
                incident.id, ACTIVE, CANCELLED, closed_at=self.clock.now(), cancel_reason=FAILED
            )
        except Exception:
            log.exception("could not close incident %s after a failed start", incident.id)

    def _fire(self, incident: Incident, call: bool, push: bool) -> None:
        """Send what is due the moment an incident starts. The arms are
        independent: neither waits for the other, and one failing does not
        stop the other."""
        jobs: List[Callable[[], Any]] = []
        record: List[Callable[[Incident, Any, Optional[BaseException]], None]] = []
        if call and self._claim_call(incident):
            jobs.append(lambda: self._place_call(incident))
            record.append(self._record_call)
        if push:
            jobs.append(lambda: self.notifier.send_emergency(incident))
            record.append(self._record_first_push)
        for note, (value, error) in zip(record, side_by_side(jobs)):
            try:
                note(incident, value, error)
            except Exception:
                log.exception("could not record an alert for %s", incident.id)

    def nothing_sent(self, incident: Incident) -> bool:
        """True while neither arm has got anything out. One arm always fires
        at the start, so right after a trigger this means that arm failed."""
        return not (incident.pushover_receipt or incident.last_call_sid)

    # ---------------------------------------------------------------- self-test

    def self_test(self, by: str, base_url: str, limited: bool = False) -> dict:
        """Run a labelled drill through the whole path, so a lapsed account or
        a broken deploy shows up on a quiet afternoon and not in an emergency.

        Returns {"incident": Incident} when one started. Otherwise nothing was
        sent and the reason is "paused", "interval" (limited callers get one a
        day) or "busy" (another incident is active, and it is left alone).
        """
        now = self.clock.now()
        paused = self.paused_seconds()
        if paused:
            log.info("self-test by %s refused: paused", by)
            return {"reason": "paused", "retry_after_seconds": paused}
        slot = "self-test:" + by
        if limited:
            taken, wait, _ = self.store.claim_slot(slot, now, AGENT_SELF_TEST_INTERVAL, 1)
            if not taken:
                log.info("self-test by %s refused: one a day, wait %ds", by, wait)
                return {"reason": "interval", "retry_after_seconds": wait}
        created = False
        try:
            incident, created = self.trigger(
                sender=SELF_TEST_SENDER, kind="self-test", base_url=base_url,
                note="A scheduled test of Emergency Ring, started by %s." % by, test=True,
            )
        finally:
            # No test started, whether it failed or another incident was in
            # the way, so a limited caller keeps its turn.
            if limited and not created:
                self.store.release_slot(slot, now)
        if not created:
            return {"reason": "busy", "retry_after_seconds": self.cfg.cycle_seconds}
        log.info("self-test %s started by %s", incident.id, by)
        return {"incident": incident}

    # -------------------------------------------------------------- break-glass

    def break_glass(
        self,
        sender: str,
        message: str,
        title: str = "",
        url: Optional[str] = None,
        idempotency_key: str = "",
        category: str = "",
        test: bool = False,
    ) -> dict:
        """An agent's request for the owner's attention.

        The first one in each interval is sent, up to the daily cap. Anything
        else is not sent at all: the caller is told why and how long to wait,
        and decides whether to hold the message and try again.

        With an idempotency key, a repeat of a request that was delivered
        returns that outcome and sends nothing. Refusals are not remembered,
        so the same key can be retried once the wait is over.

        test=True is for the monthly self-test: the alert is headed TEST and
        leaves the real window and daily count alone, so a real alert just
        after it is not held back. An agent gets one of those a day.
        """
        if not self.cfg.push_enabled:
            raise RuntimeError("break-glass needs the push arm")
        now = self.clock.now()
        paused = self.paused_seconds()
        if paused:
            log.info("break-glass sender=%s category=%s refused=paused", sender, category or "-")
            return {"delivered": False, "retry_after_seconds": paused, "reason": "paused"}
        slot = ("break-glass-test:" if test else "break-glass:") + sender
        idem = ("break-glass:%s:%s" % (sender, idempotency_key)) if idempotency_key else ""
        if idem:
            prior = self.store.idem_begin(idem, now)
            if prior is not None:
                if prior.get("state") != "done":
                    return {"in_progress": True}
                return dict(prior["result"], duplicate=True)
        try:
            if test:
                interval, cap = AGENT_SELF_TEST_INTERVAL, 1
            else:
                interval, cap = self.cfg.break_glass_interval_seconds, self.cfg.break_glass_daily_cap
            taken, wait, reason = self.store.claim_slot(slot, now, interval, cap)
            if not taken:
                log.info("break-glass sender=%s category=%s refused=%s wait=%ds",
                         sender, category or "-", reason, wait)
                if idem:
                    self.store.idem_finish(idem, None)
                return {"delivered": False, "retry_after_seconds": wait, "reason": reason}
            heading = ("%s: %s" % (sender, title.strip())) if title.strip() else sender
            if test:
                heading = "TEST · " + heading
            try:
                self.notifier.send_notice(
                    heading[:MAX_TITLE], message[:MAX_MESSAGE],
                    sound=self.cfg.break_glass_sound, url=url,
                )
            except Exception:
                # Nothing reached the phone, so the slot was not really used.
                self.store.release_slot(slot, now)
                raise
        except Exception:
            if idem:
                self.store.idem_finish(idem, None)
            raise
        log.info("break-glass sender=%s category=%s delivered", sender, category or "-")
        # A test does not close the real window, so nothing to wait for.
        result = {"delivered": True, "retry_after_seconds": 0 if test else interval}
        if idem:
            try:
                self.store.idem_finish(idem, result)
            except Exception:
                # The alert is out; say so, or the agent would send it again.
                log.exception("could not record the outcome for an idempotency key")
        return result

    def describe(self, incident: Incident) -> str:
        """One sentence on where an incident stands, for the sender's screen."""
        owner = self.cfg.owner_name
        if incident.status == ACKNOWLEDGED:
            if incident.acked_via == "call":
                return "%s answered the call and acknowledged." % self._who_acknowledged(incident)
            return "%s acknowledged the alert." % self.cfg.owner_cap
        if incident.status == CANCELLED:
            return {
                PAUSED: "Alerts are paused, so this one was stopped. Reach %s another way." % owner,
                SUPERSEDED: "Stopped: a real alert replaced this test.",
                FAILED: "The alert could not be started. Reach %s another way." % owner,
                STALLED: "This alert stalled and was replaced by a new one.",
            }.get(incident.cancel_reason or "", "Cancelled.")
        if incident.status == EXPIRED:
            return "Nobody acknowledged. The alerts have stopped; keep trying to reach %s." % owner
        if self._stalled(incident, self.clock.now()):
            return "This alert has stalled. Send it again, and reach %s another way." % owner
        if self.nothing_sent(incident):
            return "Nothing has reached %s yet. Still trying; reach them another way too." % owner
        if incident.attempts:
            return "Still alerting %s: no response yet, %s." % (owner, _call_count(incident))
        return "Still alerting %s: no response yet." % owner

    def _who_acknowledged(self, incident: Incident) -> str:
        # A keypress on a call to the backup number is not the owner's.
        by_backup = incident.acked_via == "call" and incident.attempts > self.cfg.primary_attempts
        return "The backup contact" if by_backup else self.cfg.owner_cap

    def break_glass_status(self, sender: str) -> dict:
        """What a send would meet right now. Sends nothing, takes nothing."""
        cap = self.cfg.break_glass_daily_cap
        wait, reason, sent = self.store.peek_slot("break-glass:" + sender, self.clock.now(), cap)
        status = {
            "ok": True,
            "push_enabled": self.cfg.push_enabled,
            "retry_after_seconds": wait,
            "sent_last_24h": sent,
            "daily_cap": cap,
            # So an agent can hold back a test while a real alert is running.
            # A stalled one does not count: nothing is alerting any more.
            "incident_active": self._running(),
        }
        if reason:
            status["reason"] = reason
        until, by = self.store.pause_state()
        paused = until - self.clock.now()
        if paused > 0:
            status.update(reason="paused", paused_seconds=paused, paused_until=until,
                          paused_by=by, retry_after_seconds=max(wait, paused))
        return status

    # -------------------------------------------------------------------- pause

    def paused_seconds(self) -> int:
        """Seconds of pause left; 0 when alerting is on."""
        return max(0, self.store.pause_state()[0] - self.clock.now())

    def pause(self, by: str, seconds: int) -> dict:
        """The kill switch: stop the active incident and refuse every new
        emergency and break-glass alert until the pause runs out. It always
        runs out by itself, so alerting cannot be left off by accident."""
        now = self.clock.now()
        seconds = max(1, min(int(seconds), MAX_PAUSE_SECONDS))
        # Flag first, so nothing new starts while the active one is stopped.
        self.store.set_pause(now + seconds, by)
        log.warning("paused by %s for %ds", by, seconds)
        stopped = self._stop_active("Alerts were paused by %s." % by)
        return {"paused": True, "paused_seconds": seconds, "paused_until": now + seconds,
                "paused_by": by, "stopped_incident": stopped}

    def resume(self, by: str) -> dict:
        self.store.set_pause(0)
        log.warning("resumed by %s", by)
        return {"paused": False}

    def _active_incident(self) -> Optional[Incident]:
        """The incident the pointer names, if it is still running."""
        incident_id = self.store.active_id()
        incident = self.store.get(incident_id) if incident_id else None
        return incident if incident is not None and incident.status == ACTIVE else None

    def _running(self) -> bool:
        incident = self._active_incident()
        return incident is not None and not self._stalled(incident, self.clock.now())

    def _stop_active(self, why: str) -> Optional[str]:
        incident = self._active_incident()
        told = "%s %s was not alerted further; reach them another way." % (why, self.cfg.owner_cap)
        if incident is None or not self._close_cancelled(incident, reason=PAUSED, tell=told):
            return None
        return incident.id

    def _stopped_by_pause(self, incident_id: str) -> bool:
        """A trigger can slip in while a pause is being set. Ticks and delayed
        pushes check here so such an incident never alerts."""
        if not self.paused_seconds():
            return False
        incident = self.store.get(incident_id)
        if incident is not None:
            self._close_cancelled(incident, reason=PAUSED)
        return True

    # --------------------------------------------------------------------- push

    def _record_first_push(
        self, incident: Incident, receipt: Optional[str], error: Optional[BaseException]
    ) -> None:
        if error is None:
            if receipt:
                self._keep_receipt(incident.id, receipt)
            return
        # Hand it to the queue, which redelivers until it works and parks the
        # message for the alarm if it never does.
        log.error("push alert failed for %s; queueing a retry", incident.id, exc_info=error)
        self.scheduler.schedule_push(incident.id, PUSH_RETRY_SECONDS)

    def _keep_receipt(self, incident_id: str, receipt: str) -> None:
        """Without the receipt an acknowledgment in the app cannot be
        recognised and the alert cannot be taken back, so one failed write
        is tried again before giving up."""
        try:
            self.store.update(incident_id, pushover_receipt=receipt)
        except Exception:
            log.exception("could not store the push receipt for %s; trying once more", incident_id)
            self.store.update(incident_id, pushover_receipt=receipt)

    def deliver_push(self, incident_id: str) -> str:
        """The first push when it comes from the queue: delayed because calls
        go first, or retried because the first try failed. Raising is safe:
        the queue redelivers, and an alert already sent is not sent twice."""
        if self._stopped_by_pause(incident_id):
            return "paused"
        incident = self.store.get(incident_id)
        if incident is None or incident.status != ACTIVE:
            return "stopped"
        if incident.pushover_receipt:
            return "already sent"
        receipt = self.notifier.send_emergency(incident)
        if not receipt:
            return "sent"
        self._keep_receipt(incident_id, receipt)
        # The send takes a moment. If the incident closed meanwhile, or a
        # duplicate delivery stored its own receipt, nothing will ever
        # retract this alert, so take it back now.
        latest = self.store.get(incident_id)
        if latest is None or latest.status != ACTIVE or latest.pushover_receipt != receipt:
            incident.pushover_receipt = receipt
            self._wind_down(incident, hangup=False)
            return "retracted"
        return "sent"

    # --------------------------------------------------------------------- tick

    def tick(self, incident_id: str, seq: int) -> str:
        """One redial decision. Returns what it did, for logs and tests.

        Raising here is safe: the queue redelivers the same (id, seq), and
        either nothing has been committed yet or the step is safe to repeat.
        """
        if self._stopped_by_pause(incident_id):
            return "paused"
        incident = self.store.get(incident_id)
        if incident is None or incident.status != ACTIVE:
            return "stopped"
        if seq != incident.tick_seq:
            return "stale"
        if self._push_acknowledged(incident):
            self.acknowledge(incident_id, via="pushover")
            return "acknowledged"

        now = self.clock.now()
        in_flight = (
            incident.call_in_flight_since is not None
            and now - incident.call_in_flight_since < self.cfg.call_grace
        )
        if not self.cfg.call_enabled:
            # Push only: Pushover does the repeating. The ticks exist to poll
            # for the acknowledgment and to close the incident eventually.
            if now - incident.created_at >= self.cfg.max_duration:
                action, delay = "expired", None
            else:
                action, delay = "polled", self.cfg.cycle_seconds
        elif in_flight:
            action, delay = "waiting", self.cfg.busy_recheck_seconds
        elif incident.attempts >= self.cfg.total_attempts:
            action, delay = "expired", None
        else:
            action, delay = "dialed", self.cfg.cycle_seconds

        if action == "expired":
            # The last tick, so there is no next one to fall back on. It is
            # not gated on advance_tick: the status change is conditional by
            # itself, and if it fails the redelivered tick must find the
            # incident still waiting to be closed.
            self._expire(incident)
            return action

        # Schedule the next tick before committing this one. A duplicate
        # delivery may enqueue a second copy of seq+1, but only one of them
        # can win advance_tick later.
        self.scheduler.schedule_tick(incident_id, seq + 1, delay)
        if not self.store.advance_tick(incident_id, seq, now):
            return "stale"
        if action == "dialed" and self._claim_call(incident):
            self._record_call(incident, *side_by_side([lambda: self._place_call(incident)])[0])
            self._settle(incident)
        return action

    def _claim_call(self, incident: Incident) -> bool:
        """Count the attempt before dialing, so two workers cannot both dial.
        False: acknowledged meanwhile, or another worker already has it."""
        try:
            return self.store.claim_attempt(incident.id, incident.attempts, self.clock.now())
        except Exception:
            # The tick loop comes round again; the other arm must still fire.
            log.exception("could not claim a call attempt for %s", incident.id)
            return False

    def _place_call(self, incident: Incident) -> str:
        target = self.cfg.target_for_attempt(incident.attempts)
        return self.telephony.place_call(target, incident.id, incident.base_url, self.cfg.ring_timeout)

    def _record_call(
        self, incident: Incident, sid: Optional[str], error: Optional[BaseException]
    ) -> None:
        if error is None:
            self.store.update(incident.id, last_call_sid=sid)
            return
        # The attempt is spent; the next tick tries again. Counted, so the
        # sender is not told that calls were placed when none were.
        log.error("call failed for %s", incident.id, exc_info=error)
        self.store.update(
            incident.id, call_in_flight_since=None, calls_failed=incident.calls_failed + 1
        )

    def _expire(self, incident: Incident) -> None:
        if not self.store.transition(incident.id, ACTIVE, EXPIRED, closed_at=self.clock.now()):
            return
        if not self.cfg.call_enabled:
            text = "Nobody acknowledged your alert."
        elif incident.calls_placed:
            text = "Nobody acknowledged your alert after %s." % _call_count(incident, "")
        else:
            text = "No call could be placed, and nobody acknowledged your alert."
        self._wind_down(incident, hangup=False, tell=text)

    # ------------------------------------------------------------ twilio webhooks

    def voice_twiml(self, incident_id: str) -> str:
        """What the call says when it is answered."""
        incident = self.store.get(incident_id)
        if incident is None or incident.status != ACTIVE:
            return _twiml("<Say>This alert has already been handled. Goodbye.</Say><Hangup/>")

        message = self._spoken_message(incident)
        ack_url = escape("%s/twilio/ack/%s" % (incident.base_url, incident.id))
        gather = (
            '<Gather numDigits="1" timeout="6" action="%s" method="POST">'
            "<Say>%s Press 1 to acknowledge.</Say>"
            "</Gather>" % (ack_url, escape(message))
        )
        # Repeat three times, then hang up unacknowledged. Voicemail never
        # presses 1, so a machine answering does not stop the redials.
        return _twiml(gather * 3 + "<Say>No response. I will call again.</Say><Hangup/>")

    def _spoken_message(self, incident: Incident) -> str:
        parts = ["Emergency alert from %s." % incident.sender]
        if incident.test:
            parts = ["This is a test of the emergency alert, from %s." % incident.sender]
        if self.cfg.target_for_attempt(max(incident.attempts - 1, 0)) != self.cfg.primary_number:
            parts.append("%s has not responded." % self.cfg.owner_cap)
        parts.append("Category: %s." % incident.kind)
        if incident.note:
            parts.append(incident.note)
        return " ".join(parts)

    def ack_from_call(self, incident_id: str, digits: str) -> str:
        if digits != "1":
            return self.voice_twiml(incident_id)
        self.acknowledge(incident_id, via="call")
        # Twilio can deliver the keypress twice, so look at where it ended up.
        incident = self.store.get(incident_id)
        if incident is not None and incident.status == ACKNOWLEDGED:
            return _twiml("<Say>Acknowledged. The calls will stop.</Say><Hangup/>")
        # Pressed after the alert was cancelled or had run out: do not let
        # the owner believe the sender was told.
        return _twiml(
            "<Say>This alert had already ended, so nothing was acknowledged. "
            "Please contact the sender.</Say><Hangup/>"
        )

    def call_status(self, incident_id: str, call_sid: str, status: str) -> None:
        """Twilio's end-of-call callback. Clears the in-flight marker."""
        incident = self.store.get(incident_id)
        if incident is None or incident.last_call_sid != call_sid:
            return
        log.info("call %s for %s ended: %s", call_sid, incident_id, status)
        changes: dict = {"call_in_flight_since": None}
        # "failed" is a call that never connected, as against busy or no
        # answer, where the phone was reached. Counted once: a repeated
        # callback finds the marker already cleared.
        if status == "failed" and incident.call_in_flight_since is not None:
            log.error("call %s for %s could not be connected", call_sid, incident_id)
            changes["calls_failed"] = incident.calls_failed + 1
        self.store.update(incident_id, **changes)

    # ------------------------------------------------------------------ closing

    def acknowledge(self, incident_id: str, via: str) -> bool:
        now = self.clock.now()
        if not self.store.transition(
            incident_id, ACTIVE, ACKNOWLEDGED, acked_via=via, acked_at=now, closed_at=now
        ):
            return False
        incident = self.store.get(incident_id)
        if incident:
            # Acknowledged in the app while the phone is ringing: stop the ring.
            ringing = via != "call" and incident.call_in_flight_since is not None
            self._wind_down(
                incident, hangup=ringing,
                tell="%s acknowledged your alert." % self._who_acknowledged(incident),
            )
        return True

    def acknowledge_from_push(self, incident_id: str, receipt: str) -> bool:
        """Pushover's callback. It is unsigned, so it is only a hint: the
        receipt must match, and then Pushover itself is asked to confirm.

        A failed lookup raises, so the caller answers 5xx and Pushover retries.
        """
        incident = self.store.get(incident_id)
        if incident is None or not incident.pushover_receipt:
            return False
        # Compared as bytes: the callback is unauthenticated and may hold anything.
        if not secrets.compare_digest(incident.pushover_receipt.encode(), (receipt or "").encode()):
            return False
        if not self.notifier.emergency_acknowledged(incident.pushover_receipt):
            log.warning("push callback for %s not confirmed by provider", incident_id)
            return False
        return self.acknowledge(incident_id, via="pushover")

    def _push_acknowledged(self, incident: Incident) -> bool:
        """Poll on each tick, in case the callback never arrived."""
        if not incident.pushover_receipt:
            return False
        try:
            return self.notifier.emergency_acknowledged(incident.pushover_receipt)
        except Exception:
            # Unknown is not acknowledged: keep calling.
            log.exception("push receipt lookup failed for %s", incident.id)
            return False

    def cancel(self, incident_id: str, sender: str) -> bool:
        incident = self.store.get(incident_id)
        if incident is None or incident.sender != sender:
            return False
        return self._close_cancelled(incident)

    def _close(self, incident: Incident, reason: str = "") -> bool:
        """Mark an active incident cancelled. Quick: nothing is sent."""
        fields = {"cancel_reason": reason} if reason else {}
        return self.store.transition(
            incident.id, ACTIVE, CANCELLED, closed_at=self.clock.now(), **fields
        )

    def _close_cancelled(self, incident: Incident, reason: str = "", tell: str = "") -> bool:
        if not self._close(incident, reason):
            return False
        self._wind_down(incident, hangup=True, tell=tell)
        return True

    def _wind_down(self, incident: Incident, hangup: bool, tell: str = "") -> None:
        """After an incident has closed: take back the push, stop a ringing
        call and tell the sender. Each is a slow vendor call and none depends
        on another, so they go out together; a failure is logged and the rest
        still happen."""
        jobs: List[Tuple[str, Callable[[], Any]]] = []
        if incident.pushover_receipt:
            jobs.append(("retract push", lambda: self.notifier.cancel_emergency(incident.pushover_receipt)))
        if hangup and incident.last_call_sid:
            jobs.append(("hangup", lambda: self.telephony.hangup(incident.last_call_sid)))
        if tell:
            jobs.append(("notify sender", lambda: self.notifier.notify_sender(incident.sender, tell)))
        for (what, _), (_, error) in zip(jobs, side_by_side([job for _, job in jobs])):
            if error is not None:
                log.error("could not %s for %s", what, incident.id, exc_info=error)


def _calls(count: int) -> str:
    return "%d call%s" % (count, "" if count == 1 else "s")


def _call_count(incident: Incident, placed: str = " placed") -> str:
    """"3 calls placed", and the ones that never connected if there were any:
    a sender should not be told the phone rang five times when it rang twice."""
    text = _calls(incident.calls_placed) + placed
    if incident.calls_failed:
        text += " (%d more could not be placed)" % incident.calls_failed
    return text


def _twiml(body: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>%s</Response>' % body
