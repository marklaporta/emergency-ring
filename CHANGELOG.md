# Changelog

## 0.1.0

First public release.

- Two alert arms that sound through silent mode and Focus on an iPhone, each
  of which can be switched off: a Pushover critical alert that says who it is
  from and what kind, with a map link, and a phone call from a Twilio number
  saved as an Emergency Bypass contact.
- Both arms repeat until someone acknowledges, by pressing 1 on the call or
  in the Pushover app, and an acknowledgment on either stops both. An
  unanswered incident ends by itself.
- A backup number to ring when the owner does not respond.
- A status sentence for the sender that counts the calls placed and those
  that never connected, and an optional Pushover message telling them how
  their alert ended.
- One incident at a time. A second trigger joins the active one, except that
  a real trigger replaces a running test, and any trigger replaces an
  incident that has stalled. A test never replaces a real alert.
- A real alert that has not reached the phone is answered `502` without an
  `id`, so the sender's Shortcut sends its own alert as well; the backend
  keeps trying.
- An iOS Shortcut for the sender, generated or built by hand, with a direct
  Pushover alert as its fallback when the backend fails. The generated drill
  Shortcut has no such fallback and carries only the sender's token.
- Labelled tests that follow the real path: a quiet drill and a rehearsal
  with the real sound.
- A kill switch that stops the active incident and refuses new alerts, and
  always ends by itself.
- Break-glass: a lower tier that lets an agent send one gentle push, never a
  call, limited per agent by a minimum interval and a daily cap.
- A monthly self-test, run by a timer in the stack or by an agent.
- Optional email alarms (the function failed, a caller got a server error, an
  alert or call could not be sent, a queued message was parked) and an AWS
  budget alert.
- Timing, sounds and limits as template parameters, checked at deploy time.
  The function's log is kept for 90 days.
- Scripts for a live drill and the kill switch, which take the stack, region,
  secrets prefix and sender from `ER_STACK`, `ER_REGION`, `ER_PREFIX` and
  `ER_SENDER`.
- An offline test suite, a virtual-time simulation and a coverage report
  that need only Python, run by a GitHub Actions workflow.
