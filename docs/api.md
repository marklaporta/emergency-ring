# HTTP API

All paths hang off the Function URL. Bearer tokens go in an
`Authorization: Bearer <token>` header.

| Route | Who may call | Replies |
|---|---|---|
| `POST /trigger` | Sender token | `201` a new incident; `200` joined the active one; `502` a real alert that has not reached the phone yet; `503` paused |
| `GET /incidents/{id}` | Either token: a sender reads any incident, an agent reads self-tests only | `200`; `404` |
| `POST /incidents/{id}/cancel` | Sender token, and only the sender who triggered it | `200` `{"cancelled": true}`; `409` `{"cancelled": false}` |
| `POST /pause` | Either token | `200`; `400` if `minutes` is not a number |
| `POST /resume` | Either token | `200` `{"paused": false}` |
| `POST /self-test` | Either token | `201` started; `429` paused, or an agent's second in a day; `409` another incident is active |
| `POST /break-glass` | Agent token | `200` delivered; `429` refused; `409`, `400`, `503`: see [docs/break-glass.md](break-glass.md) |
| `GET /break-glass/status` | Agent token | `200` |
| `POST /twilio/voice/{id}` | No token: Twilio's request signature | `200` with what the call should say; `403` |
| `POST /twilio/ack/{id}` | No token: Twilio's request signature | `200`; `403` |
| `POST /twilio/status/{id}` | No token: Twilio's request signature | `200`; `403` |
| `POST /pushover/ack/{id}` | No token: unsigned, so confirmed with Pushover before it counts | `200` |

On every route with a token, a missing or wrong token, or one of the wrong
kind, gets `401`. A body that should be JSON and is not gets `400`, an unknown
path `404`, and a failure inside the function `500`. A refusal for a pause, a
rate limit or another incident in the way carries a `Retry-After` header and
`retry_after_seconds`.

**`POST /trigger`.** The body and all its fields are optional:

```json
{"kind": "kids", "note": "Call me now.", "lat": 51.5007, "lon": -0.1246}
```

`kind` is free text; it defaults to `emergency` and is cut to 40 characters.
`note` is cut to 200; the provided Shortcuts do not send one, so their alert
reads "Emergency alert" under a title with the sender and the kind. A
location is used only if both halves are valid.

A `test` field makes it a test only when it is exactly `true`, `"drill"` or
`"full"`; see [Tests and drills](../README.md#tests-and-drills). Any other value,
including `"true"`, `1` and `"Drill"`, starts a real alert.

The reply to a trigger, and to `GET /incidents/{id}`:

```json
{"id": "EXAMPLE-incident-id", "status": "active", "kind": "kids", "attempts": 0,
 "calls_failed": 0, "created_at": 1767268800, "acked_via": null, "acked_at": null,
 "test": false, "rehearsal": false,
 "summary": "Still alerting Alex: no response yet.", "created": true}
```

- `status` is `active`, `acknowledged`, `expired` or `cancelled`.
- `attempts` counts calls tried. `calls_failed` counts those that never
  connected: calls Twilio refused to place, and calls it later reported as
  failed.
- `acked_via` is `call` or `pushover`.
- `created` (trigger only) is false when the request joined an incident that
  was already active.
- `summary` is a sentence for the sender's screen. While the incident is
  active it reads, for example, "Still alerting Alex: no response yet, 2 calls
  placed (1 more could not be placed)." Once the incident is over the same
  sentence is also returned as `final`, so a Shortcut can test for "has any
  value". Examples: "Alex answered the call and acknowledged.", "The backup
  contact answered the call and acknowledged." and "Nobody acknowledged. The
  alerts have stopped; keep trying to reach Alex."
- A cancelled incident's `summary` says why: "Cancelled.", "Alerts are
  paused, so this one was stopped. Reach Alex another way.", "Stopped: a real
  alert replaced this test.", "This alert stalled and was replaced by a new
  one." or "The alert could not be started. Reach Alex another way."

When a real alert has not reached the phone, the backend answers `502`, whether
the request started the incident or joined it:

```json
{"error": "no alert has gone out yet", "incident_id": "EXAMPLE-incident-id",
 "summary": "Nothing has reached Alex yet. Still trying; reach them another way too."}
```

The incident stays active and keeps trying. The field is `incident_id`, not
`id`, on purpose: the sender's Shortcut looks for `id`, and without it sends
its direct alert as well. A test in the same state gets the ordinary `201` or
`200` reply, with a `summary` that says nothing has reached the owner, so a
failed drill does not set off a Shortcut's backup alert. While paused the
reply is `503` with `{"error": "paused", "retry_after_seconds": ...}`.

**`POST /incidents/{id}/cancel`** works for the sender whose trigger started
the incident. A self-test belongs to no sender, so only an acknowledgment or a
pause stops it.

**`POST /pause`** takes an optional `{"minutes": 60}` and answers with
`paused`, `paused_seconds`, `paused_until` (epoch seconds), `paused_by` (the
name on the token) and `stopped_incident` (the id of the incident it
cancelled, or null).

**`POST /self-test`** takes no body. When it starts a test the reply is the
incident, as above, with `"started": true`. A refusal has `"started": false`
and a `reason`: `paused`, `interval` or `busy`. It is `busy` when another
incident is in the way, and a real alert always is, stalled or not: a test
never takes a real alert's place.

**Break-glass** requests and replies, including `"test": true` and the
`incident_active` field of the status check, are in
[docs/break-glass.md](break-glass.md).
