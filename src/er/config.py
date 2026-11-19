from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

log = logging.getLogger("er")

PUSH = "push"
CALL = "call"
# Shorter than this and a token could be guessed over a public URL.
MIN_TOKEN_LENGTH = 20

# Pushover refuses emergency alerts that repeat faster than this, or that stay
# alive for longer than three hours.
PUSHOVER_MIN_RETRY = 30
PUSHOVER_MAX_EXPIRE = 10800
# An SQS message can be delayed by at most 15 minutes.
MAX_DELAY = 900


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = (env.get(name) or "").strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError("%s must be true or false, got %r" % (name, value))


# Environment variable -> (Config field, how to read its text). A variable
# that is unset or empty leaves the field at its default, so each default is
# written once, on the field.
_SETTINGS: Dict[str, Tuple[str, Callable[[str], Any]]] = {
    "OWNER_NAME": ("owner_name", str),
    "PRIMARY_NUMBER": ("primary_number", str),
    "BACKUP_NUMBER": ("backup_number", str),
    "FIRST_ARM": ("first_arm", lambda text: text.strip().lower()),
    "ARM_OFFSET_SECONDS": ("arm_offset_seconds", int),
    "CYCLE_SECONDS": ("cycle_seconds", int),
    "PRIMARY_ATTEMPTS": ("primary_attempts", int),
    "BACKUP_ATTEMPTS": ("backup_attempts", int),
    "RING_TIMEOUT": ("ring_timeout", int),
    "PUSH_SOUND": ("push_sound", str),
    "TEST_PUSH_SOUND": ("test_push_sound", str),
    "BREAK_GLASS_MINUTES": ("break_glass_interval_seconds", lambda text: 60 * int(text)),
    "BREAK_GLASS_SOUND": ("break_glass_sound", str),
    "BREAK_GLASS_DAILY_CAP": ("break_glass_daily_cap", int),
    "SENDER_PUSHOVER_KEYS": ("sender_pushover_keys", lambda text: _sender_keys(text)),
}


def _entry(name: str) -> str:
    """A name for a log line. A long one may be a token put in the wrong
    place, so it is never printed."""
    return repr(name) if len(name) < MIN_TOKEN_LENGTH else "an entry with a long name"


def _agent_tokens(text: str) -> Dict[str, str]:
    """The agents' list is optional and must never be able to stop an
    emergency, so one that is not JSON is logged and read as empty."""
    try:
        return json.loads(text or "{}")
    except ValueError:
        log.error("break-glass-tokens is not JSON; no agent can alert until it is fixed")
        return {}


def _sender_keys(text: str) -> Dict[str, str]:
    """Sender receipts are a convenience. A value that cannot be read (easy
    to get wrong: a deploy tool that cuts JSON at the first quote) is logged
    and ignored, so it cannot take the emergency path down with it."""
    try:
        keys = json.loads(text)
        if isinstance(keys, dict) and all(isinstance(key, str) for key in keys.values()):
            return keys
    except ValueError:
        pass
    log.error("SENDER_PUSHOVER_KEYS is not a JSON object of name to user key; "
              "senders will not be told when an alert is acknowledged")
    return {}


@dataclass(frozen=True)
class Config:
    # Who gets called.
    owner_name: str = "the owner"  # spoken and shown to senders; set a first name
    primary_number: str = ""
    backup_number: Optional[str] = None

    # The two alert arms. Either can be switched off; at least one must be on.
    call_enabled: bool = True   # phone call from the Emergency Bypass number
    push_enabled: bool = True   # Pushover critical alert

    # When both arms are on: which goes first, and how long the other waits.
    # An offset of 0 fires both at once.
    first_arm: str = PUSH
    arm_offset_seconds: int = 30
    # Each arm repeats on this interval until someone acknowledges.
    cycle_seconds: int = 90

    # Call behaviour.
    primary_attempts: int = 5
    backup_attempts: int = 3
    ring_timeout: int = 25          # hang up before voicemail answers
    in_flight_grace: int = 120      # max time we trust "a call is in progress"
    busy_recheck_seconds: int = 15  # re-tick delay while a call is in progress

    # Push behaviour. See https://pushover.net/api#sounds for the names; the
    # ones marked "long" there suit an alarm.
    push_sound: str = "persistent"
    # Sound for drills (a trigger sent with "test": true). The volume of a
    # critical alert is set in the Pushover app, not by the sender, so a
    # gentler sound is the only way the backend can make a drill quieter.
    # "vibrate" and "none" are valid too.
    test_push_sound: str = "pushover"

    # Break-glass: a lower tier for an automated agent that may interrupt the
    # owner about something that needs attention now but is not an emergency.
    # One push, never a call, never repeated. At most one alert per sender per
    # interval; anything sent sooner is refused and never reaches the phone.
    break_glass_interval_seconds: int = 15 * 60
    break_glass_sound: str = "magic"
    # A backstop against a runaway agent: alerts per agent in any 24 hours.
    # 0 means no cap.
    break_glass_daily_cap: int = 8
    # agent name -> bearer token. Kept apart from trigger_tokens so an agent's
    # token can never start a full emergency.
    break_glass_tokens: Dict[str, str] = field(default_factory=dict, repr=False)

    # Incident lifetime.
    active_pointer_ttl: int = 2 * 3600
    record_ttl: int = 90 * 24 * 3600

    # sender name -> bearer token. The sender is derived from the token,
    # never from the request body.
    trigger_tokens: Dict[str, str] = field(default_factory=dict, repr=False)
    # sender name -> Pushover user key, for "acknowledged" receipts.
    sender_pushover_keys: Dict[str, str] = field(default_factory=dict)
    # Checks the signature on Twilio's webhooks.
    twilio_auth_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not (self.call_enabled or self.push_enabled):
            raise ValueError("at least one of the call and push arms must be enabled")
        if self.first_arm not in (PUSH, CALL):
            raise ValueError("first_arm must be 'push' or 'call', got %r" % self.first_arm)
        if not 0 <= self.arm_offset_seconds <= MAX_DELAY:
            raise ValueError("arm_offset_seconds must be between 0 and %d" % MAX_DELAY)
        if not PUSHOVER_MIN_RETRY <= self.cycle_seconds <= MAX_DELAY:
            raise ValueError(
                "cycle_seconds must be between %d and %d" % (PUSHOVER_MIN_RETRY, MAX_DELAY)
            )
        if self.break_glass_interval_seconds < 0:
            raise ValueError("break_glass_interval_seconds cannot be negative")
        if self.break_glass_daily_cap < 0:
            raise ValueError("break_glass_daily_cap cannot be negative")
        if self.primary_attempts < 1 or self.backup_attempts < 0:
            raise ValueError("primary_attempts must be at least 1 and backup_attempts at least 0")
        if not 5 <= self.ring_timeout <= 60:
            raise ValueError("ring_timeout must be between 5 and 60 seconds")
        # A blank is how a deploy tool spells "no backup number".
        object.__setattr__(self, "backup_number", (self.backup_number or "").strip() or None)
        if self.push_delay and self.push_delay >= self.max_duration:
            raise ValueError("arm_offset_seconds is so long that the calls would end before the push is sent")
        if self.call_enabled and not self.primary_number:
            raise ValueError("primary_number is required when the call arm is enabled")
        self._check_tokens()

    def _check_tokens(self) -> None:
        """Say clearly what is wrong with a token list, without ever printing
        a token. A sender list that cannot work is an error. Everything else
        is logged and worked around: refusing to start would take the
        emergency path down, and an agent's list must never be able to."""
        self._check_list("trigger-tokens", self.trigger_tokens, strict=True)
        self._check_list("break-glass-tokens", self.break_glass_tokens, strict=False)
        if not any(self.trigger_tokens.values()):
            log.error("trigger-tokens has no usable token: nobody can start an emergency")
        senders = {token: name for name, token in self.trigger_tokens.items() if token}
        for name, token in self.break_glass_tokens.items():
            if token in senders:
                log.error(
                    "break-glass-tokens: %s has the same token as the sender %s, "
                    "so that agent can start an emergency", _entry(name), _entry(senders[token]),
                )

    def _check_list(self, label: str, tokens: Dict[str, str], strict: bool) -> None:
        problem = None
        if not isinstance(tokens, dict):
            problem = "%s must be a JSON object of name to token" % label
            tokens = {}
        usable = {}
        for position, (name, token) in enumerate(tokens.items(), 1):
            if not name or not isinstance(token, str):
                problem = "%s: entry %d must be a name with a text token" % (label, position)
            elif len(name) >= MIN_TOKEN_LENGTH > len(token):
                # Written the wrong way round, so the "name" is the token.
                problem = "%s: entry %d looks like token-to-name; it must be name-to-token" % (label, position)
            else:
                usable[name] = token
                if 0 < len(token) < MIN_TOKEN_LENGTH:
                    log.warning("%s: the token for %s is short enough to guess", label, _entry(name))
        if problem and strict:
            raise ValueError(problem)
        if problem:
            log.error("%s; the entries that cannot be read are ignored", problem)
            object.__setattr__(self, "break_glass_tokens", usable)

    # ------------------------------------------------------------- scheduling

    @property
    def owner_cap(self) -> str:
        """The owner's name as it reads at the start of a sentence."""
        return self.owner_name[:1].upper() + self.owner_name[1:]

    @property
    def both_arms(self) -> bool:
        return self.call_enabled and self.push_enabled

    @property
    def call_delay(self) -> int:
        """Seconds after the trigger before the first call."""
        if self.both_arms and self.first_arm == PUSH:
            return self.arm_offset_seconds
        return 0

    @property
    def push_delay(self) -> int:
        """Seconds after the trigger before the first push."""
        if self.both_arms and self.first_arm == CALL:
            return self.arm_offset_seconds
        return 0

    @property
    def total_attempts(self) -> int:
        """How many alert cycles run before the incident expires."""
        return self.primary_attempts + (self.backup_attempts if self.backup_number else 0)

    @property
    def call_grace(self) -> int:
        """How long a call is taken to be in progress when its end-of-call
        callback has not arrived: the ring, then the message read three
        times. Redials are never closer together than this."""
        return max(self.in_flight_grace, self.ring_timeout + 100)

    @property
    def pointer_ttl(self) -> int:
        """How long the active-incident pointer outlives its incident's
        start. Longer than any incident can run, so a second one cannot
        start beside it."""
        longest = self.arm_offset_seconds + self.max_duration + self.abandoned_after
        return max(self.active_pointer_ttl, longest)

    @property
    def abandoned_after(self) -> int:
        """How long without a tick before an active incident counts as dead.
        A tick is at most one cycle (or the arm offset) away, and the queue
        redelivers a failed one five times, half a minute apart."""
        return max(self.cycle_seconds, self.arm_offset_seconds) + 300

    @property
    def max_duration(self) -> int:
        """Longest an incident stays active, used directly when only the
        push arm is on and there are no call attempts to count."""
        return self.total_attempts * self.cycle_seconds

    @property
    def pushover_retry(self) -> int:
        # The push repeats on the same cycle as the calls. A cycle shorter
        # than Pushover allows was already refused when this was built.
        return self.cycle_seconds

    @property
    def pushover_expire(self) -> int:
        # Outlive the whole incident, so the alert can still be acknowledged
        # in the app right up to the end: an expired alert cannot be.
        wanted = self.max_duration + self.arm_offset_seconds + 2 * self.cycle_seconds
        return min(wanted, PUSHOVER_MAX_EXPIRE)

    def target_for_attempt(self, attempt_index: int) -> str:
        """attempt_index is 0-based."""
        if attempt_index < self.primary_attempts or not self.backup_number:
            return self.primary_number
        return self.backup_number

    # ------------------------------------------------------------------- env

    @staticmethod
    def arms_from_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, bool]:
        """Which arms are switched on. Read before the secrets are loaded,
        because it decides which secrets are required."""
        env = os.environ if env is None else env
        return {
            CALL: _flag(env, "CALL_ENABLED", True),
            PUSH: _flag(env, "PUSH_ENABLED", True),
        }

    @classmethod
    def from_env(
        cls,
        trigger_tokens: str = "",
        twilio_auth_token: str = "",
        break_glass_tokens: str = "",
        env: Optional[Mapping[str, str]] = None,
    ) -> "Config":
        """Plain settings come from the environment; the secrets are passed
        in by the caller, which reads them from SSM."""
        env = os.environ if env is None else env
        arms = cls.arms_from_env(env)
        settings = {
            attribute: read(env[variable])
            for variable, (attribute, read) in _SETTINGS.items() if env.get(variable)
        }
        return cls(
            call_enabled=arms[CALL],
            push_enabled=arms[PUSH],
            trigger_tokens=json.loads(trigger_tokens or "{}"),
            break_glass_tokens=_agent_tokens(break_glass_tokens),
            twilio_auth_token=twilio_auth_token,
            **settings
        )
