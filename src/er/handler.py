"""Lambda entry point.

Three kinds of event arrive here:
  - HTTP requests through the Function URL (a sender, an agent, Twilio, Pushover)
  - SQS messages: redial ticks, and a first push that was delayed or is being
    retried
  - a scheduled {"action": "selftest"} event
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import math
import os
import re
import urllib.parse
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

from .config import Config
from .core import SELF_TEST_SENDER, Paused, Service
from .twilio_adapter import signature_is_valid

log = logging.getLogger("er")
log.setLevel(logging.INFO)

Response = Dict[str, Any]

_service: Optional[Service] = None


def build_service() -> Service:
    from .aws_adapters import DynamoStore, SqsScheduler
    from .config import CALL, PUSH
    from .pushover_adapter import NullNotifier, PushoverNotifier
    from .twilio_adapter import NullTelephony, TwilioTelephony

    from .ssm_secrets import (
        BREAK_GLASS_TOKENS, PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY, TRIGGER_TOKENS,
        TWILIO_AUTH_TOKEN, load_secrets, required_for,
    )

    env = os.environ
    # The arm switches decide which secrets must exist, so read them first.
    arms = Config.arms_from_env(env)
    secrets = load_secrets(env["SSM_PREFIX"], required=required_for(arms[CALL], arms[PUSH]))
    config = Config.from_env(
        trigger_tokens=secrets[TRIGGER_TOKENS],
        twilio_auth_token=secrets.get(TWILIO_AUTH_TOKEN, "") if arms[CALL] else "",
        break_glass_tokens=secrets.get(BREAK_GLASS_TOKENS, ""),
    )
    if config.push_enabled:
        notifier: Any = PushoverNotifier(
            secrets[PUSHOVER_APP_TOKEN],
            secrets[PUSHOVER_USER_KEY],
            config.sender_pushover_keys,
            retry=config.pushover_retry,
            expire=config.pushover_expire,
            sound=config.push_sound,
            test_sound=config.test_push_sound,
        )
    else:
        log.info("push arm is off; calls only")
        notifier = NullNotifier()
    if config.call_enabled:
        for name in ("TWILIO_ACCOUNT_SID", "TWILIO_FROM_NUMBER"):
            if not env.get(name):
                raise RuntimeError("%s is required when the call arm is enabled" % name)
        telephony: Any = TwilioTelephony(
            env["TWILIO_ACCOUNT_SID"], config.twilio_auth_token, env["TWILIO_FROM_NUMBER"]
        )
    else:
        log.info("call arm is off; push only")
        telephony = NullTelephony()
    return Service(
        config,
        DynamoStore(env["TABLE_NAME"], config),
        telephony,
        notifier,
        SqsScheduler(env["TICK_QUEUE_URL"]),
    )


def _get() -> Service:
    global _service
    if _service is None:
        _service = build_service()
    return _service


def install(service: Service) -> None:
    """Tests use this to swap in a service built on fakes."""
    global _service
    _service = service


# ------------------------------------------------------------------- responses

def _json(status: int, body: Dict[str, Any], retry_after: Optional[int] = None) -> Response:
    headers = {"content-type": "application/json"}
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    return {"statusCode": status, "headers": headers, "body": json.dumps(body)}


def _xml(body: str) -> Response:
    return {"statusCode": 200, "headers": {"content-type": "text/xml"}, "body": body}


# -------------------------------------------------------------------- requests

class BadRequest(Exception):
    """The request cannot be read. The message is shown to the caller."""


class Request:
    def __init__(self, event: Dict[str, Any]) -> None:
        http = event.get("requestContext", {}).get("http", {})
        self.method = http.get("method", "GET").upper()
        self.path = event.get("rawPath", "/")
        self.query = event.get("rawQueryString", "")
        self.headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            # Anyone can post anything here; junk must end in a 4xx, not a crash.
            body = base64.b64decode(body).decode("utf-8", "replace")
        self.body = body
        domain = event.get("requestContext", {}).get("domainName", "")
        # No trailing slash: Twilio signs the exact URL it was given, and the
        # callback URLs are built as base + path.
        self.base_url = (os.environ.get("PUBLIC_BASE_URL") or ("https://" + domain)).rstrip("/")

    @property
    def url(self) -> str:
        return self.base_url + self.path + ("?" + self.query if self.query else "")

    def form(self) -> Dict[str, str]:
        parsed = urllib.parse.parse_qs(self.body, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def json(self) -> Dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body)
        except ValueError:
            raise BadRequest("body must be JSON") from None
        # Any other JSON value counts as an empty object, so an oddly shaped
        # body from a valid sender still starts an alert.
        return data if isinstance(data, dict) else {}

    def bearer(self) -> str:
        value = self.headers.get("authorization", "")
        return value[7:].strip() if value.lower().startswith("bearer ") else ""


class Caller(NamedTuple):
    name: str
    is_sender: bool  # False for an agent


# Who may use a route. The two token lists are kept apart so that an agent's
# token can never start an emergency.
SENDER, AGENT, ANYONE = "sender", "agent", "anyone"


def _token_owner(token: str, tokens: Dict[str, str]) -> Optional[str]:
    """Map a bearer token to its owner's name, comparing every entry in
    constant time."""
    found = None
    for name, expected in tokens.items():
        if expected and hmac.compare_digest(expected.encode(), token.encode()):
            found = name
    return found


def _caller(token: str, config: Config, allowed: str) -> Optional[Caller]:
    """Who holds this token, if they may use a route open to `allowed`. The
    sender list is asked first, so a token on both lists is a sender's."""
    if allowed in (SENDER, ANYONE):
        sender = _token_owner(token, config.trigger_tokens)
        if sender:
            return Caller(sender, True)
    if allowed in (AGENT, ANYONE):
        agent = _token_owner(token, config.break_glass_tokens)
        if agent:
            return Caller(agent, False)
    return None


def _clean_coords(lat: Any, lon: Any) -> Tuple[Optional[str], Optional[str]]:
    """A point on the map as two strings, or nothing at all: half a location,
    or one off the globe, would only make a dead link."""
    try:
        if isinstance(lat, bool) or isinstance(lon, bool):
            raise ValueError
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180):
        return None, None
    return "%.6f" % lat, "%.6f" % lon


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _clean_text(value: Any) -> str:
    """Caller text is spoken by Twilio from an XML document, and one control
    character makes the whole document invalid."""
    return _CONTROL.sub(" ", str(value or "")).strip()


# ---------------------------------------------------------------------- routes

def _trigger(req: Request, service: Service, caller: Caller) -> Response:
    body = req.json()
    mode = body.get("test")
    lat, lon = _clean_coords(body.get("lat"), body.get("lon"))
    try:
        incident, created = service.trigger(
            sender=caller.name,
            kind=_clean_text(body.get("kind")),
            note=_clean_text(body.get("note")),
            lat=lat,
            lon=lon,
            base_url=req.base_url,
            # "test": true or "drill" is a quiet drill; "full" is a rehearsal
            # with the real sound. Anything else is a real alert.
            test=mode is True or mode in ("drill", "full"),
            rehearsal=mode == "full",
        )
    except Paused as paused:
        # The reply has no "id", which is what the sender's Shortcut looks
        # for, so it sends its backup alert.
        return _json(503, {"error": "paused", "retry_after_seconds": paused.seconds},
                     retry_after=paused.seconds)
    return _started(service, incident, created=created)


def _started(service: Service, incident: Any, **extra: Any) -> Response:
    """The reply to whoever started an incident, or joined the active one."""
    summary = service.describe(incident)
    if service.nothing_sent(incident) and not incident.test:
        # Nothing has reached the phone. The incident stays active and keeps
        # trying, but the caller must not take that for success: there is no
        # "id" here, which is what the sender's Shortcut looks for, so it
        # sends its backup alert as well. A test gets the ordinary reply:
        # its summary says what happened, and a failed drill must not set
        # off a real backup alert.
        return _json(502, {"error": "no alert has gone out yet", "incident_id": incident.id,
                           "summary": summary})
    fresh = extra.get("created", True)
    return _json(201 if fresh else 200, dict(incident.public_view(), summary=summary, **extra))


def _refused(result: Dict[str, Any], **extra: Any) -> Response:
    """The reply when the service sent nothing and said why: a pause, a rate
    limit, or another incident in the way. Says how long to wait."""
    status = 409 if result["reason"] == "busy" else 429
    return _json(status, dict(result, **extra), retry_after=result["retry_after_seconds"])


DEFAULT_PAUSE_MINUTES = 60


def _pause(req: Request, service: Service, caller: Caller) -> Response:
    body = req.json()
    try:
        minutes = int(body.get("minutes") or DEFAULT_PAUSE_MINUTES)
    except (TypeError, ValueError, OverflowError):
        return _json(400, {"error": "minutes must be a number"})
    # The service holds it to a day at most.
    return _json(200, service.pause(caller.name, max(1, minutes) * 60))


def _resume(req: Request, service: Service, caller: Caller) -> Response:
    return _json(200, service.resume(caller.name))


# Keeps a caller-supplied label from forging or breaking a log line.
_LOG_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")
# Pushover's limit for a link.
MAX_URL = 512
_UNSAFE_IN_URL = re.compile(r"[\s\x00-\x1f\x7f]")


def _break_glass(req: Request, service: Service, caller: Caller) -> Response:
    body = req.json()
    message = str(body.get("message") or "").strip()
    if not message:
        return _json(400, {"error": "message is required"})
    url = str(body.get("url") or "").strip() or None
    if url and not url.startswith("https://"):
        return _json(400, {"error": "url must start with https://"})
    if url and (len(url) > MAX_URL or _UNSAFE_IN_URL.search(url)):
        return _json(400, {"error": "url is too long or has spaces in it"})
    if not service.cfg.push_enabled:
        return _json(503, {"error": "the push arm is off"})
    key = str(body.get("idempotency_key") or "").strip()
    if len(key) > 100:
        return _json(400, {"error": "idempotency_key is too long"})
    result = service.break_glass(
        caller.name, message, title=str(body.get("title") or ""), url=url,
        idempotency_key=key, category=_LOG_UNSAFE.sub("", str(body.get("category") or ""))[:40],
        test=body.get("test") is True,
    )
    if result.get("in_progress"):
        return _json(409, {"error": "a request with this idempotency_key is still in progress"})
    if result["delivered"]:
        return _json(200, result)
    return _refused(result)


def _break_glass_status(req: Request, service: Service, caller: Caller) -> Response:
    return _json(200, service.break_glass_status(caller.name))


def _self_test(req: Request, service: Service, caller: Caller) -> Response:
    """Lets an agent run the monthly test, so it can warn the owner first and
    report the result afterwards. This door only ever starts a labelled drill."""
    result = service.self_test(caller.name, req.base_url, limited=not caller.is_sender)
    incident = result.get("incident")
    if incident is None:
        return _refused(result, started=False)
    return _started(service, incident, started=True)


def _status(req: Request, service: Service, caller: Caller, incident_id: str) -> Response:
    incident = service.store.get(incident_id)
    # An agent may follow a self-test and nothing else.
    self_test = incident is not None and incident.test and incident.sender == SELF_TEST_SENDER
    if incident is None or not (caller.is_sender or self_test):
        return _json(404, {"error": "not found"})
    view = dict(incident.public_view(), summary=service.describe(incident))
    if incident.status != "active":
        # Present only once the incident is over: a Shortcut can test for
        # "has any value" without comparing strings.
        view["final"] = view["summary"]
    return _json(200, view)


def _cancel(req: Request, service: Service, caller: Caller, incident_id: str) -> Response:
    cancelled = service.cancel(incident_id, caller.name)
    return _json(200 if cancelled else 409, {"cancelled": cancelled})


def _twilio(handler: Callable[[Service, str, Dict[str, str]], Response]) -> Callable[..., Response]:
    def wrapped(req: Request, service: Service, incident_id: str) -> Response:
        form = req.form()
        token = service.cfg.twilio_auth_token
        signature = req.headers.get("x-twilio-signature", "")
        if not token or not signature_is_valid(token, req.url, form, signature):
            log.warning("rejected a Twilio request to %s: no valid signature", req.path)
            return _json(403, {"error": "bad signature"})
        return handler(service, incident_id, form)

    return wrapped


@_twilio
def _twilio_voice(service: Service, incident_id: str, form: Dict[str, str]) -> Response:
    return _xml(service.voice_twiml(incident_id))


@_twilio
def _twilio_ack(service: Service, incident_id: str, form: Dict[str, str]) -> Response:
    return _xml(service.ack_from_call(incident_id, form.get("Digits", "")))


@_twilio
def _twilio_status(service: Service, incident_id: str, form: Dict[str, str]) -> Response:
    service.call_status(incident_id, form.get("CallSid", ""), form.get("CallStatus", ""))
    return _json(200, {"ok": True})


def _pushover_ack(req: Request, service: Service, incident_id: str) -> Response:
    form = req.form()
    if form.get("acknowledged") != "1":
        return _json(200, {"ok": True})
    acknowledged = service.acknowledge_from_push(incident_id, form.get("receipt", ""))
    return _json(200, {"acknowledged": acknowledged})


_ID = r"([A-Za-z0-9_-]{8,40})"
# Method, path, who may call, route. Any sender or agent may pause and resume:
# stopping the noise is the safe direction, and a pause always ends by itself.
# The routes marked None check the caller themselves: Twilio signs its
# requests, and Pushover's callback counts only once Pushover confirms it.
ROUTES = [
    ("POST", re.compile(r"^/trigger$"), SENDER, _trigger),
    ("POST", re.compile(r"^/pause$"), ANYONE, _pause),
    ("POST", re.compile(r"^/resume$"), ANYONE, _resume),
    ("POST", re.compile(r"^/self-test$"), ANYONE, _self_test),
    ("POST", re.compile(r"^/break-glass$"), AGENT, _break_glass),
    ("GET", re.compile(r"^/break-glass/status$"), AGENT, _break_glass_status),
    ("GET", re.compile(r"^/incidents/%s$" % _ID), ANYONE, _status),
    ("POST", re.compile(r"^/incidents/%s/cancel$" % _ID), SENDER, _cancel),
    ("POST", re.compile(r"^/twilio/voice/%s$" % _ID), None, _twilio_voice),
    ("POST", re.compile(r"^/twilio/ack/%s$" % _ID), None, _twilio_ack),
    ("POST", re.compile(r"^/twilio/status/%s$" % _ID), None, _twilio_status),
    ("POST", re.compile(r"^/pushover/ack/%s$" % _ID), None, _pushover_ack),
]


def _handle_http(event: Dict[str, Any]) -> Response:
    service = _get()
    req = Request(event)
    for method, pattern, who, route in ROUTES:
        match = pattern.match(req.path)
        if not match or method != req.method:
            continue
        if who is None:
            return route(req, service, *match.groups())
        caller = _caller(req.bearer(), service.cfg, who)
        if caller is None:
            return _json(401, {"error": "unauthorized"})
        return route(req, service, caller, *match.groups())
    return _json(404, {"error": "not found"})


def _handle_queue(records: Any) -> Dict[str, Any]:
    """Redial ticks and queued first pushes. Nothing is caught here: an
    exception makes SQS deliver the message again."""
    service = _get()
    for record in records:
        message = json.loads(record["body"])
        if message.get("action") == "push":
            result = service.deliver_push(message["incident_id"])
            log.info("push %s -> %s", message["incident_id"], result)
            continue
        result = service.tick(message["incident_id"], int(message["seq"]))
        log.info("tick %s seq=%s -> %s", message["incident_id"], message["seq"], result)
    return {"ok": True}


def _handle_schedule() -> Dict[str, Any]:
    """The stack's own timer asking for the self-test."""
    service = _get()
    base_url = os.environ.get("PUBLIC_BASE_URL", "")
    if not base_url:
        raise RuntimeError("PUBLIC_BASE_URL must be set for the self-test")
    result = service.self_test("the schedule", base_url)
    incident = result.pop("incident", None)
    if incident is None:
        return result
    if service.nothing_sent(incident):
        # Nobody reads this reply. Raising is what reaches the error alarm.
        raise RuntimeError("self-test %s: no alert could be sent" % incident.id)
    return {"incident": incident.id}


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Any:
    if "Records" in event:
        return _handle_queue(event["Records"])
    if event.get("action") == "selftest":
        return _handle_schedule()
    try:
        return _handle_http(event)
    except BadRequest as error:
        return _json(400, {"error": str(error)})
    except Exception:
        log.exception("unhandled error")
        return _json(500, {"error": "internal error"})


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    # On Lambda, load the secrets and build the clients while the environment
    # starts, not inside the first request: that request may be the alert, or
    # Twilio waiting for what to say on a call that is already ringing. If it
    # fails here, the first request tries again and reports the error.
    try:
        _get()
    except Exception:
        log.exception("could not start during init; the first request will retry")
