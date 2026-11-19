# Emergency Ring

Design notes for anyone working on this code, human or AI.

Emergency Ring lets a trusted person make someone's iPhone sound through
silent mode and any Focus, for real emergencies only. `README.md` has the
purpose, the quick start, the flow and the limits; `docs/` has the full
set-up, the settings, the HTTP routes and troubleshooting.
The reasons behind the design are kept here and nowhere else, and the README
links to this file for them. Notes about one particular deployment (names,
accounts, decisions, test history) belong in `CLAUDE.local.md`, which is
gitignored; read it first if it exists.

## Why it is built this way

- iOS only makes sound through silent mode and Focus for Critical Alerts and
  for contacts with Emergency Bypass. An app cannot send Critical Alerts
  without an Apple-granted entitlement, so there are two alert arms that need
  none: a Pushover emergency-priority alert (Pushover holds the entitlement),
  and a phone call from a dedicated Twilio number saved as an Emergency Bypass
  contact. Either arm can be switched off.
- Acknowledgment is a keypress on the call or a Pushover acknowledgment, never
  "call answered": Twilio reports voicemail pickups as completed calls. An
  acknowledgment on either arm stops both.
- The Pushover callback is unsigned, so it is only a hint; the receipt API is
  asked to confirm. An expired Pushover alert can no longer be acknowledged,
  so `pushover_expire` is derived to outlive the incident.
- AWS serverless: Lambda with a Function URL, DynamoDB, and an SQS delay queue
  as the redial timer (more precise than EventBridge Scheduler, and it
  redelivers failed ticks).
- The Lambda has no third-party dependencies (stdlib `urllib` for Twilio and
  Pushover; `boto3` comes from the runtime). Keep it that way.
- The trigger must be easy under stress and hard to fire by accident: a Home
  Screen Shortcut that opens a menu before sending. No Action Button, Back Tap
  or Lock Screen widget triggers.
- Break-glass (`POST /break-glass`) is a second, lower tier for an automated
  agent: one gentle push, never a call, never repeated, rate-limited per
  agent, with its own token list so an agent can never start an emergency. A
  refused alert is not delivered in any form.
- `POST /pause` is the kill switch: it cancels the active incident and
  refuses all alerts until the pause expires. It always expires (a day at
  most), and any sender or agent token may use it.
- `POST /self-test` starts a labelled drill and nothing else, so an agent
  can run the monthly test: warn the owner, start it, report the result. An
  agent token gets one a day and can read self-tests only. A timer in the
  stack can run it instead (`SelfTestTimer`).
- Tests never displace real alerts. A real trigger replaces a running test at
  once, and the real alert goes out before the test's alerts are taken back.
  A test that meets a real incident joins it, even a stalled one.
- A stalled incident (no tick for `Config.abandoned_after`, or nothing sent a
  minute after every arm was due) is replaced by the next trigger. It is
  never closed from a read path such as a status request, because Pushover
  may still be repeating its alert.
- A reply to a trigger carries `id` only when it is safe for the sender's
  Shortcut to stop there. A real incident that has sent nothing answers `502`
  with `incident_id`, so the Shortcut sends its direct alert too. A test in
  that state gets the ordinary reply, so a failed drill cannot set off a real
  backup alert.
- Optional conveniences must not take the emergency path down: an unreadable
  `SenderPushoverKeys` or a bad entry in `break-glass-tokens` is logged and
  ignored. A sender list that cannot work is an error.
- Timing and behaviour choices are template parameters with sensible
  defaults, not constants in code.

## Priorities

It must reach the owner in a real emergency; it must never fail silently; it
must not cry wolf; then everything else. Keep the machinery in proportion: for
most people the cost of this being down is being back where they were before
they had it. Prefer the simple version.

## Conventions

- `src/er/core.py` holds every decision and imports no cloud or vendor code.
  Adapters implement the interfaces in `ports.py`; `fakes.py` mirrors them.
- Every status, tick and attempt change is a conditional update, and the
  break-glass slot is guarded by its `ver`, because SQS and Twilio can both
  deliver twice. Other fields on an incident are written without a version
  check, to an existing record only. The pause is written unconditionally.
- An incident can close while an alert or call for it is still being sent.
  Whoever sends reads the record back afterwards (`_settle`) and takes back
  what went out too late.
- Local code must run on Python 3.9 (`from __future__ import annotations`, no
  `X | None` at runtime). Lambda runs 3.13.
- Tests use stdlib `unittest`; there is no pytest. Example names in tests and
  docs are neutral: Robin sends, Alex (or "the owner") is alerted, and the
  agent is called Assistant. Keep real names, numbers and account ids out of
  tracked files.
- Never print or log a token. Check one by shape and fingerprint.
- `tests/test_docs.py` holds the tables in `docs/settings.md` and `docs/api.md` to
  `template.yaml` and `handler.ROUTES`, and scans every tracked file for
  real-looking Function URLs, Twilio account ids and AWS access keys. Change
  the docs in the same commit as a parameter or a route.

## Commands

- Tests: `python3 -m unittest discover -s tests` (one file:
  `python3 tests/test_core.py`)
- Coverage: `python3 scripts/coverage.py [--min N]`
- Simulation: `python3 scripts/simulate.py [scenario]`
- Template check: `sam validate --lint --region us-west-2`
- Deploy: `sam deploy`, with a local `samconfig.toml` copied from
  `samconfig.example.toml`. No `sam build` is needed.
- Live test: `scripts/drill.sh` (quiet drill) or `scripts/drill.sh full`
  (rehearsal with the real sound). `ER_STACK`, `ER_REGION`, `ER_PREFIX` and
  `ER_SENDER` point the scripts at another deployment or sender.
