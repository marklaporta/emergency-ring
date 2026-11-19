# Deploy

The rest of the set-up, in any order after the
[quick start](../README.md#quick-start-push-only).

## The call arm

1. Twilio: open an account and upgrade it from trial. A trial account plays a
   trial message before yours and can only call verified numbers. Buy a number
   that can place voice calls, and turn on auto-recharge so the balance cannot
   run out unnoticed.
2. Store the Twilio auth token as the `twilio-auth-token` secret.
3. In `samconfig.toml` set `CallEnabled=true`, `TwilioAccountSid`,
   `TwilioFromNumber` and `PrimaryNumber`, and `BackupNumber` if someone else
   should be rung when the owner does not respond. Run `sam deploy`. The
   template refuses a call arm without the account, the Twilio number or the
   number to ring. To stop ringing the backup number later, write
   `'BackupNumber=""'`: deleting the line keeps the old number, as
   `samconfig.example.toml` explains.
4. On the owner's iPhone, save the Twilio number as a contact and turn on
   Emergency Bypass for its ringtone (Contacts > the contact > Ringtone >
   Emergency Bypass). A ringtone of its own helps. Turn on Wi-Fi Calling.
5. Keep that number to yourself. Emergency Bypass goes by the caller's number
   and caller ID can be faked, so expect any call that shows this number to
   ring through silent mode. Such a call cannot trigger or acknowledge
   anything in the backend.
6. Set `RingTimeout` (5 to 60 seconds) below the time your carrier's voicemail
   takes to pick up.
7. Rehearse with `scripts/drill.sh full` and press 1 when the call comes.

## Alarms, sender receipts, break-glass

- **Failure alarms and a budget alert.** Set `BudgetAlertEmail`. Four alarms
  then email that address: the function raised an error, it answered a caller
  with a server error, an alert or call could not be sent, or a queued message
  failed five times and landed in the dead-letter queue. AWS sends a
  confirmation link first, and the alarms stay silent until you click it. [When
  an alarm email arrives](troubleshooting.md#when-an-alarm-email-arrives) says what to do with
  one. The same address gets an AWS budget alert at 50% and 100% of
  `MonthlyBudgetUsd` and when the forecast passes it. The budget covers the
  whole AWS account and does not see Twilio or Pushover charges.
- **Telling the sender how it ended.** The Shortcut watches an incident for
  three minutes. To reach the sender after that, give `SenderPushoverKeys` a
  JSON map of sender name to that sender's own Pushover user key. They then get
  a Pushover message when their alert is acknowledged, when it expires
  unanswered, or when a pause stops it. Without it nothing tells them that an
  unanswered alert has stopped. It needs the push arm. The value contains
  quotes, and `sam deploy` cuts it short unless you wrap the line as
  `samconfig.example.toml` shows; the template refuses a value that is not a
  JSON object. This parameter is a secret; see [Secrets](#secrets).
- **Break-glass.** Optional; see [docs/break-glass.md](break-glass.md).
- **The monthly self-test.** See [Monthly self-test](../README.md#monthly-self-test).

## Secrets

The secrets live in SSM Parameter Store. The function reads the first five when
it starts, and its role may only read parameters under the `SecretsPrefix` path
(default `/emergency-ring`). CloudFormation cannot create SecureString
parameters, so you create them by hand, in the same region as the stack.

| Parameter | Value |
|---|---|
| `/emergency-ring/trigger-tokens` | JSON map of sender name to bearer token, e.g. `{"Robin":"..."}`. Always required. |
| `/emergency-ring/pushover-app-token` | Pushover application token. Required for the push arm. |
| `/emergency-ring/pushover-user-key` | The owner's Pushover user key. Required for the push arm. |
| `/emergency-ring/twilio-auth-token` | Twilio auth token. Required for the call arm. |
| `/emergency-ring/break-glass-tokens` | JSON map of agent name to bearer token. Optional; without it break-glass is closed. |
| `/emergency-ring/shortcut-pushover-app-token` | A second Pushover application token, for the sender's Shortcut alone. Optional. Read by `scripts/make_shortcuts.py`, not by the function; see [docs/shortcut.md](shortcut.md). |

Copy the value to the clipboard, then run this once per parameter. Reading it
from the clipboard keeps it out of the shell history (`pbpaste` is macOS; use
your own clipboard tool elsewhere):

```bash
aws ssm put-parameter --region us-west-2 --type SecureString --name /emergency-ring/trigger-tokens --value "$(pbpaste)"
```

Four more things about secrets:

- **One secret is a deploy parameter.** `SenderPushoverKeys` holds Pushover
  user keys, and anyone with a user key can push to that person's phone.
  CloudFormation hides the value, but it sits in `samconfig.toml` and the
  function holds it as a plain environment variable, readable in the Lambda
  console. The other parameters are not secrets, but they include phone numbers
  and an email address, so keep `samconfig.toml` private either way.
- **The two token lists must not share a token.** The function treats a token
  on both lists as a sender's, so that agent could start an emergency. It logs
  an error at start when it finds one. It also warns about a token shorter than
  20 characters.
- **A list that cannot be read.** `trigger-tokens` must be a JSON object of
  name to token, or the function does not start. The function logs and ignores
  a `break-glass-tokens` value or entry it cannot read, so a mistake there
  never stops emergencies; it stops only the agent's alerts, until you fix it.
- **Changing one.** Add `--overwrite` to the command above. A running function
  keeps the values it read when it started, which can be hours ago while
  requests keep arriving, and a `sam deploy` with nothing changed does not
  restart it. To make it read the new value, deploy once with a setting that
  the function reads changed (`TestPushSound`, say). Then try the old token on
  a request that sends nothing: `GET /incidents/00000000` for a sender's token,
  or `GET /break-glass/status` for an agent's. Both should answer 401.

## Uninstall

`sam delete` removes the stack, including the incident table and the logs.
Then delete the SSM parameters, release the Twilio number, and remove the
contact and the Shortcuts from the phones.
