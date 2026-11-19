"""Secrets live in SSM Parameter Store as SecureString parameters under one
prefix, and are read once per cold start.

  <prefix>/trigger-tokens       always required; JSON map of sender name -> bearer token
  <prefix>/twilio-auth-token    required when the call arm is on
  <prefix>/pushover-app-token   required when the push arm is on
  <prefix>/pushover-user-key    required when the push arm is on
  <prefix>/break-glass-tokens   optional; JSON map of agent name -> bearer token.
                                Without it the break-glass endpoint is closed.
"""
from __future__ import annotations

from typing import Any, Dict, Sequence, Tuple

TWILIO_AUTH_TOKEN = "twilio-auth-token"
TRIGGER_TOKENS = "trigger-tokens"
PUSHOVER_APP_TOKEN = "pushover-app-token"
PUSHOVER_USER_KEY = "pushover-user-key"
BREAK_GLASS_TOKENS = "break-glass-tokens"

NAMES = (
    TRIGGER_TOKENS, TWILIO_AUTH_TOKEN, PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY, BREAK_GLASS_TOKENS,
)


def required_for(call_enabled: bool, push_enabled: bool) -> Tuple[str, ...]:
    """The parameters a deployment cannot run without, given its arms."""
    names = [TRIGGER_TOKENS]
    if call_enabled:
        names.append(TWILIO_AUTH_TOKEN)
    if push_enabled:
        names += [PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY]
    return tuple(names)


def load_secrets(prefix: str, required: Sequence[str], client: Any = None) -> Dict[str, str]:
    """Returns {short name: value} for every parameter that exists.

    Raises if a required one is missing: a Lambda that cannot check a bearer
    token, or that is missing an alert arm it was told to use, must fail
    loudly, not run half-configured.
    """
    prefix = prefix.rstrip("/")
    if not prefix.startswith("/"):
        raise RuntimeError("SSM_PREFIX must start with '/', got %r" % prefix)
    if client is None:
        import boto3  # provided by the Lambda runtime

        from .aws_adapters import client_config

        client = boto3.client("ssm", config=client_config())
    names = ["%s/%s" % (prefix, short) for short in NAMES]
    response = client.get_parameters(Names=names, WithDecryption=True)
    found = {
        parameter["Name"][len(prefix) + 1:]: parameter["Value"]
        for parameter in response.get("Parameters", [])
    }
    missing = [short for short in required if not found.get(short)]
    if missing:
        raise RuntimeError(
            "missing SSM parameters: " + ", ".join("%s/%s" % (prefix, m) for m in missing)
        )
    return found
