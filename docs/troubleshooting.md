# Troubleshooting

Start with the function's log. It is the CloudWatch log group named after
the stack, `/emergency-ring/function`:

```bash
aws logs tail /emergency-ring/function --since 15m --region us-west-2
```

- **Every request answers 500.** The function could not start. The log names
  the cause: `missing SSM parameters` (a secret was not created, or is in
  another region or under another prefix), `AccessDenied` (for instance a
  secret encrypted with a KMS key of your own; the role is set up for the
  default key only), `primary_number is required when the call arm is
  enabled`, `TWILIO_ACCOUNT_SID is required when the call arm is enabled`, a
  token list that cannot be read (see [Secrets](deploy.md#secrets)), or a setting
  outside its limits. The messages are in `src/er/config.py`,
  `src/er/handler.py` and `src/er/ssm_secrets.py`. A `SenderPushoverKeys`
  value that cannot be read is not among the causes: it is logged and
  ignored, and senders are simply not told how their alerts ended.
- **The URL answers 403 before the function runs.** A public Function URL
  needs two permissions on the function, `lambda:InvokeFunctionUrl` and
  `lambda:InvokeFunction`. Look at the stack's resources in the
  CloudFormation console: there should be two of type
  `AWS::Lambda::Permission` for the URL. If one is missing, deploy again.
- **The phone rings, but the call does not speak the message or take the
  keypress, and the log says `rejected a Twilio request`.** Twilio's webhooks
  are getting 403 because `PublicBaseUrl` does not match the Function URL.
  Correct it.
- **Calls open with a trial message, or only reach some numbers.** The Twilio
  account is still a trial. Upgrade it.
- **The call does not ring through silent mode.** The Twilio number must be
  saved as a contact with Emergency Bypass on for the ringtone.
- **The phone rang quietly or only vibrated.** Two things to check. iOS
  Attention Aware Features lowers alert volume while you look at the phone, so
  test without looking at it. And the ringer may be low: raise Settings >
  Sounds & Haptics > Ringtone and Alerts.
- **A critical alert made no sound.** This happened with the Pushover app open
  on screen; test with the phone locked. Then check that Critical Alerts are
  enabled in the Pushover app for emergency priority, and separately for high
  priority if break-glass alerts should sound through silent mode. Their
  volume is set in the same place.
- **Calls go to voicemail.** With Wi-Fi but no cell signal, the call needs
  Wi-Fi Calling to be on. Otherwise `RingTimeout` may be longer than the
  carrier's voicemail pickup.
- **`sam deploy` refuses the parameters.** The template checks them: at least
  one arm on, the three call-arm values present when `CallEnabled=true`,
  `PublicBaseUrl` without a trailing slash and not empty when
  `SelfTestTimer=true`, the ranges in the [Settings](settings.md#settings) tables, and
  `SenderPushoverKeys` a JSON object. If that last one is refused although
  you wrote an object, the value was cut short at a quote or a space on its
  way in; `samconfig.example.toml` shows the quoting that works.
- **A setting you removed from `samconfig.toml` is still in force.** SAM keeps
  the previous value of a parameter that is left out. State the value you
  want, as `samconfig.example.toml` describes.

## When an alarm email arrives

With `BudgetAlertEmail` set, four alarms can email you. Each covers the five
minutes before it fired. Look in the function's log, above, around that time.

- **The function raised an error.** Most often a queued tick or push failed,
  or the timer's self-test could not send its first alert. The queue tries a
  failed message again, so one email followed by a normal incident needs
  nothing more.
- **A caller was answered with a server error.** Some request got a `500`,
  `502` or `503`. That includes a trigger refused during a pause, and a real
  alert that had not reached the phone when the reply was sent.
- **An alert or call could not be sent.** Pushover or Twilio refused or
  failed. The log line names the incident. The incident carries on with
  whatever still works, so check the accounts: a lapsed number, an empty
  balance, a revoked token.
- **A queued message was parked.** A tick or a push failed five times and now
  sits in the dead-letter queue, named in the stack's `DeadLetterQueue`
  output. Its incident has stopped redialling. This alarm stays in alarm, and
  sends no further email, until that queue is empty. Read the message in the
  SQS console (it holds an incident id and nothing else of note), find the
  cause in the log, then purge the queue there. A message left alone is
  deleted after 14 days.
