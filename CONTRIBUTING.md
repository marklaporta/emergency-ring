# Contributing

Issues and small pull requests are welcome. This is a small project maintained
in spare time, so there is no support promise and replies can be slow.

Security problems go through a private report, not an issue: see
[SECURITY.md](SECURITY.md).

## Running the tests

Python 3.9 or later is all you need. Nothing is installed and nothing leaves
the machine. From the repository's top folder:

```bash
python3 -m unittest discover -s tests
python3 scripts/simulate.py
```

The first must pass. The second plays incidents out in virtual time and is the
quickest way to see what a change does to the timing.

- That first command is the supported way to run the suite. Other forms,
  such as `python3 -m unittest tests.test_core`, do not find the shared
  `tests/support.py`. One file runs with `python3 tests/test_core.py`.
- A passing run prints log lines and tracebacks: the tests provoke failures
  on purpose and the code logs them. The run passed if its last line starts with `OK`.
- `python3 scripts/coverage.py` reports line coverage of `src/er` using only
  the standard library, and `--min N` makes it fail below N percent.
- A GitHub Actions workflow, `.github/workflows/tests.yml`, runs the unit
  tests on Python 3.9 and 3.13 for every push and pull request.

Tests never reach a live system. `scripts/drill.sh`, `scripts/pause.sh` and
`scripts/make_shortcuts.py` alert a real phone or read real secrets, so a test
may only run them against stand-ins.

## Rules the code keeps

- **Standard library only.** The function has no third-party dependencies;
  `boto3` comes from the Lambda runtime and is imported only where it is used.
  Tests are `unittest`, with no pytest.
- **Python 3.9 locally.** Use `from __future__ import annotations`, and no
  `X | None` at runtime. Lambda itself runs 3.13.
- **Neutral names.** In tests, docs and examples Robin sends, Alex (or "the
  owner") is alerted, and the agent is called Assistant. No real names, phone
  numbers, account ids, addresses or Function URLs in tracked files.
- **No token is printed or logged**, in code, tests or scripts.
- **`src/er/core.py` decides everything** and imports no cloud or vendor code.
  Adapters implement the interfaces in `ports.py`, and `fakes.py` mirrors
  them.
- **Docs change with the code.** `tests/test_docs.py` fails when a route or a
  template parameter is missing from `docs/settings.md` or `docs/api.md`. It also scans every tracked
  file for things shaped like a real Function URL, a Twilio account id or an
  AWS access key.

[CLAUDE.md](CLAUDE.md) explains why it is built the way it is, and the
priorities: reach the owner, never fail silently, do not cry wolf, and keep
the machinery in proportion.

## What fits

- A fix for a bug, with a test that fails without it.
- A correction to the docs, above all from running it on a real phone: an iOS
  version where a step moved, a setting that mattered, a failure you met.
- A test for a failure mode that has none.

Please open an issue before starting on a new feature, another vendor or
platform, or anything that adds a dependency or a moving part. The answer may
be no: the project prefers the simple version.

## Where things are

| Path | What it is |
|---|---|
| `src/er/core.py` | The incident state machine |
| `src/er/handler.py` | Lambda entry point and the HTTP routes |
| `src/er/config.py`, `models.py`, `limits.py` | Settings, the incident record, rate-limit arithmetic |
| `src/er/ports.py` | The interfaces the adapters implement |
| `src/er/aws_adapters.py` | DynamoDB store and SQS timer |
| `src/er/twilio_adapter.py` | Outbound calls and the webhook signature check |
| `src/er/pushover_adapter.py` | Emergency push, retraction, sender receipts |
| `src/er/ssm_secrets.py` | Reads the secrets from SSM Parameter Store |
| `src/er/fakes.py` | In-memory stand-ins and the virtual-time simulation |
| `template.yaml` | The AWS SAM template |
| `scripts/drill.sh`, `scripts/pause.sh` | A live drill and the kill switch; both source `scripts/lib.sh`, which finds the stack and a sender's token |
| `scripts/make_shortcuts.py` | The Shortcut generator |
| `scripts/simulate.py`, `scripts/coverage.py` | The virtual-time simulation and the coverage report |
| `tests/` | The unit tests |
| `.github/workflows/tests.yml` | Runs the tests on every push and pull request |
| `docs/` | The diagram, the Shortcut guide and the break-glass guide |

By contributing you agree that your work is released under the project's MIT
licence.
