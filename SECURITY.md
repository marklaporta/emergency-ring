# Security

## Reporting a vulnerability

Please report security problems privately. On the repository's **Security**
tab, **Report a vulnerability** opens a private security advisory.

If that button is not there, open an issue that says only "I have a security
report", with no details, and a private channel will be arranged. Do not put
the problem itself in a public issue.

In the private report, say what the problem is, how to reproduce it and which
commit you looked at. Leave real tokens, phone numbers and Function URLs out;
placeholders are enough.

This is a small project maintained in spare time, so there is no promised
response time.

## What is in scope

The code and the documented set-up in this repository: the function in
`src/er`, `template.yaml`, the scripts and the Shortcuts they generate. For
example:

- starting an emergency, or acknowledging one, without the right to;
- an agent's token doing more than [docs/break-glass.md](docs/break-glass.md)
  lists;
- getting past the break-glass limits;
- a secret reaching a log, a reply or a generated file where it should not be.

Out of scope: faults in AWS, Twilio, Pushover or iOS themselves, which belong
with those vendors; anyone's own deployment, since the authors run no service
for others; and the limits listed below, which are known.

## Threat model

- **The Function URL is public.** Anyone can send it requests. Everything a
  person or an agent can do needs a bearer token, compared in constant time.
  Nothing limits wrong guesses, so the tokens must be long and random; the
  function warns about one shorter than 20 characters. A flood of requests
  costs money, which is what the budget alert is for.
- **Two kinds of token, kept apart.** A sender's token can start an emergency
  or a self-test, read incidents, cancel its own, and pause and resume. An
  agent's token can send rate-limited break-glass alerts, pause and resume,
  start one labelled self-test a day and read self-tests. It can never start
  an emergency or read a real one, and a test never takes the place of a real
  alert. The sender's name comes from the token, never from the request. A
  token put on both lists counts as a sender's; the function logs an error
  when it starts with one.
- **Twilio's webhooks are signed.** The function checks Twilio's signature
  over the exact URL and fields, and answers 403 without it. A keypress
  acknowledgment can only arrive this way.
- **Pushover's callback is unsigned,** so it is only a hint. The receipt in it
  must match the incident's, and then Pushover's receipt API is asked to
  confirm before the incident closes.
- **The agent tier is rate-limited** per agent: a minimum interval and a daily
  cap, one push, never a call. An agent reads text written by strangers, so
  its instructions must treat message content as information and never as a
  command; docs/break-glass.md has the wording.
- **Secrets** live in SSM Parameter Store as SecureString parameters, and the
  function's role can read only those under its prefix. The exception is
  `SenderPushoverKeys`, a deploy parameter that the function holds as a plain
  environment variable. No token is logged.

Known limits:

- Whoever holds any token can keep alerts paused, a day at a time. Stopping
  the noise was chosen as the safe direction. The remedy for a leaked token is
  to replace it.
- The sender's Shortcut holds their token and the Pushover keys in plain form,
  and iCloud syncs it to their other devices. The owner's Pushover user key
  is one of them, and anyone who has it can send the owner alerts;
  [docs/shortcut.md](docs/shortcut.md) says what to do when a phone is lost.
- Emergency Bypass goes by the caller's number, and caller ID can be faked. A
  call that shows the Twilio number should be expected to ring through silent
  mode, so do not publish that number. Such a call cannot trigger or
  acknowledge anything.
- Nothing here defends against someone with access to the AWS account, or
  with a sender's unlocked phone.
