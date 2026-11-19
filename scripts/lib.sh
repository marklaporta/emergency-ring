# Sourced by drill.sh and pause.sh, not run by itself: finds the deployed
# backend and sends it requests as one of the senders.
#
# Settings, all optional:
#   ER_STACK   the CloudFormation stack         (emergency-ring)
#   ER_REGION  the region it is deployed in     (us-west-2)
#   ER_PREFIX  where its SSM parameters live    (/emergency-ring)
#   ER_SENDER  whose token to use               (the first sender in trigger-tokens)
#
# The token stays in a shell variable and reaches curl on standard input. A
# command line can be read by every process on the machine, so the token is
# never put on one, and it is not printed. The variable is not exported, so
# child processes do not inherit it either.

er_die() { echo "${0##*/}: $*" >&2; exit 1; }

# Sets ER_URL (ending in a slash), ER_NAME (the sender whose token is used)
# and ER_TOKEN, or stops and says what is missing.
er_lookup() {
  # Whatever the caller's environment held under these names, start clean and
  # keep them out of every child's environment.
  unset ER_URL ER_NAME ER_TOKEN
  local stack="${ER_STACK:-emergency-ring}" region="${ER_REGION:-us-west-2}"
  local prefix="${ER_PREFIX:-/emergency-ring}"

  ER_URL=$(aws cloudformation describe-stacks --region "$region" --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue" --output text) \
    || er_die "could not read the stack $stack in $region: is it deployed, and is the AWS CLI signed in?"
  # The token goes to whatever this says, so it has to be the backend and
  # nothing else: one https address.
  case "$ER_URL" in
    ''|None) er_die "the stack $stack in $region has no FunctionUrl output" ;;
    *[[:space:]]*) er_die "the FunctionUrl output of $stack is not one address" ;;
    https://?*) ;;
    *) er_die "the backend address is not https, so the token is not sent to it: $ER_URL" ;;
  esac
  case "$ER_URL" in */) ;; *) ER_URL="$ER_URL/" ;; esac

  # Only the chosen token leaves this pipeline. An empty input means aws has
  # already said what went wrong.
  local found
  found=$(aws ssm get-parameter --region "$region" --with-decryption \
    --name "$prefix/trigger-tokens" --query Parameter.Value --output text \
    | ER_SENDER="${ER_SENDER:-}" python3 -c '
import json, os, sys
text = sys.stdin.read()
if not text.strip():
    sys.exit(1)
try:
    tokens = json.loads(text)
except ValueError:
    tokens = None
if not isinstance(tokens, dict) or not tokens:
    sys.exit("trigger-tokens is not a JSON object of sender names and tokens")
name = os.environ["ER_SENDER"] or next(iter(tokens))
if name not in tokens:
    sys.exit("no sender %r in trigger-tokens; have: %s" % (name, ", ".join(tokens)))
token = tokens[name]
if not isinstance(token, str) or not token or any(c.isspace() for c in token):
    sys.exit("the token for %r is empty or has spaces in it" % name)
print(name)
print(token)') \
    || er_die "could not get a sender's token from $prefix/trigger-tokens in $region"
  ER_NAME="${found%%$'\n'*}"
  ER_TOKEN="${found#*$'\n'}"
}

# er_send SECONDS METHOD PATH [JSON]
# Sets REPLY_STATUS and REPLY_BODY. An HTTP error is still an answer, so the
# caller decides what it means. Returns non-zero when there was no answer.
er_send() {
  local out
  local -a args
  # -q first: without it curl reads ~/.curlrc, where a "verbose" line would
  # print the token and an "include" line would break the reply parsing.
  args=(-q -sS -m "$1" --proto '=https' -X "$2" -H @- -w '\n%{http_code}')
  if [ $# -ge 4 ]; then
    args+=(-H 'Content-Type: application/json' -d "$4")
  fi
  out=$(printf 'Authorization: Bearer %s\n' "$ER_TOKEN" | curl "${args[@]}" "${ER_URL}$3") || return 1
  REPLY_STATUS="${out##*$'\n'}"
  REPLY_BODY="${out%$'\n'*}"
}

# The same, but no answer at all stops the script.
er_request() {
  er_send "$@" || er_die "no answer from the backend at ${ER_URL}$3"
}

er_ok() { case "$REPLY_STATUS" in 2??) return 0 ;; *) return 1 ;; esac; }
