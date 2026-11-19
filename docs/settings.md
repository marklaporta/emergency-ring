# Settings

All of these are parameters on the SAM template. "empty" means the default is
an empty value.

Who is alerted, and the accounts:

| Parameter | Default | Allowed | What it does |
|---|---|---|---|
| `OwnerName` | `the owner` | Text | The person being alerted, as spoken on calls and shown to senders. A first name reads best. |
| `PrimaryNumber` | empty | A number like `+15555550123` | The phone to ring. Required when the call arm is on. |
| `BackupNumber` | empty | A number in the same form | A second phone to ring when the first does not respond. Empty or blank means none. |
| `TwilioAccountSid` | empty | Text | Your Twilio account. Required when the call arm is on. |
| `TwilioFromNumber` | empty | A number in the same form | The Twilio number that calls, saved on the owner's phone as the Emergency Bypass contact. Required when the call arm is on. |
| `SecretsPrefix` | `/emergency-ring` | A path such as `/name` | Where the [secrets](deploy.md#secrets) live in SSM Parameter Store. |
| `SenderPushoverKeys` | `{}` | A JSON object | Sender name to that sender's Pushover user key, so they are told how their alert ended. A secret. `samconfig.example.toml` shows how to quote it. |
| `PublicBaseUrl` | empty | Empty, or `https://` and a host with nothing after it | The `FunctionUrl` output without its trailing slash. Only the self-test timer needs it. If set, it must be exactly that URL: Twilio is sent there for what to say, and its requests are checked against it. |

The two arms and their timing:

| Parameter | Default | Allowed | What it does |
|---|---|---|---|
| `CallEnabled` | `true` | `true` or `false` | The phone-call arm. |
| `PushEnabled` | `true` | `true` or `false` | The Pushover arm. At least one arm must be on. |
| `FirstArm` | `push` | `push` or `call` | With both on, which fires first. |
| `ArmOffsetSeconds` | `30` | 0 to 900 | How long the second arm waits. 0 fires both at once. |
| `CycleSeconds` | `90` | 30 to 900 | How often each arm repeats. |
| `PrimaryAttempts` | `5` | 1 or more | Cycles aimed at the primary number. |
| `BackupAttempts` | `3` | 0 or more | Further cycles aimed at `BackupNumber`, if set. |
| `RingTimeout` | `25` | 5 to 60 | Seconds each call rings. Keep it under your voicemail pickup. |

Sounds and break-glass:

| Parameter | Default | Allowed | What it does |
|---|---|---|---|
| `PushSound` | `persistent` | A [Pushover sound name](https://pushover.net/api#sounds) | The alarm. The ones Pushover marks "long" suit it. Listen to your choice with `scripts/drill.sh full`. |
| `TestPushSound` | `pushover` | A Pushover sound name | The sound for drills. `vibrate` and `none` also work. |
| `BreakGlassSound` | `magic` | A Pushover sound name | The break-glass sound. Keep it gentle. |
| `BreakGlassMinutes` | `15` | 0 or more | The shortest time between two alerts from one agent. |
| `BreakGlassDailyCap` | `8` | 0 or more | Alerts per agent in any 24 hours. 0 means no cap. |

The self-test timer, alarms and budget:

| Parameter | Default | Allowed | What it does |
|---|---|---|---|
| `SelfTestTimer` | `false` | `true` or `false` | Run the monthly self-test from a timer in the stack. Needs `PublicBaseUrl`; the template refuses `true` without it. |
| `SelfTestSchedule` | `cron(0 14 ? * 7#1 *)` | An EventBridge Scheduler expression | When the timer fires. The default is 14:00 on the first Saturday of the month. |
| `SelfTestTimezone` | `UTC` | An IANA time zone | The zone the schedule is read in, such as `Europe/London`. |
| `BudgetAlertEmail` | empty | An email address | Where the four failure alarms and the budget alert go. Empty skips both. |
| `MonthlyBudgetUsd` | `5` | 1 or more | The monthly AWS spend, in US dollars, that the budget alert watches. |

Choosing the timing:

- The defaults leave 30 seconds to act on the push before the phone rings, and
  let a call answered on its last ring finish before the push repeats.
- With one arm off, the other starts at once.
- An unanswered incident lasts `PrimaryAttempts` cycles, plus `BackupAttempts`
  cycles when `BackupNumber` is set, plus the offset when both arms are on and
  the push goes first. The same count of cycles applies with the push arm
  alone, where no calls are placed.
- A call is never placed while the last one is in progress. If Twilio's
  end-of-call report goes missing, the next call waits until `RingTimeout`
  plus 100 seconds after the last one was placed (125 seconds with the
  default, and never less than 120), whatever `CycleSeconds` says.
- With the call first, an `ArmOffsetSeconds` so long that the calls would end
  before the push is sent is refused: the function does not start.
- Pushover stops repeating an alert after three hours at most. Settings that
  make an incident longer than that leave its last part without a repeating
  push.
