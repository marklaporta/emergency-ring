#!/bin/bash
# Trigger a test incident against the deployed stack and wait for it to close.
#
#   scripts/drill.sh                    quiet drill: test sound, labelled TEST
#   scripts/drill.sh full               rehearsal: the real sound, labelled TEST
#   scripts/drill.sh drill "a note"     with your own message text
#
# This alerts a real phone. It reads the endpoint from CloudFormation and the
# sender's token from SSM, and does not print the token. Settings, all
# optional (lib.sh has the details):
#   ER_STACK (emergency-ring)  ER_REGION (us-west-2)  ER_PREFIX (/emergency-ring)
#   ER_SENDER (the first sender in trigger-tokens)
set -euo pipefail
MODE="${1:-drill}"
NOTE="${2:-Emergency Ring $MODE. Acknowledge to stop it.}"
case "$MODE" in drill|full) ;; *) echo "usage: $0 [drill|full] [note]" >&2; exit 2;; esac

case "$0" in */*) HERE="${0%/*}" ;; *) HERE=. ;; esac
. "$HERE/lib.sh"
er_lookup
echo "sending as the sender $ER_NAME" >&2

BODY=$(python3 -c 'import json, sys; print(json.dumps({"kind": "test", "note": sys.argv[2], "test": sys.argv[1]}))' "$MODE" "$NOTE")
er_send 30 POST trigger "$BODY" \
  || er_die "no answer from the backend; a $MODE may have started all the same"
# A reply without an id means no drill is running: paused or refused. Show
# what the backend said instead of waiting on nothing. "joined" means another
# incident was already active, and this request changed nothing about it.
STARTED=$(printf '%s' "$REPLY_BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d["id"], "started" if d.get("created", True) else "joined")
except (ValueError, KeyError, TypeError):
    sys.exit(1)') || {
  printf '%s\n' "$REPLY_BODY" >&2
  er_die "the backend did not start a $MODE (HTTP $REPLY_STATUS)"
}
ID="${STARTED%% *}"
case "$STARTED" in
  *joined) er_die "another incident is already active ($ID); your $MODE was not started" ;;
esac
echo "started $MODE $ID"
MISSED=0
for ((i = 1; i <= 60; i++)); do
  # One request that gets no answer is a missed look, not the end of the
  # drill: the phone is still alerting.
  if ! er_send 15 GET "incidents/$ID"; then
    MISSED=$((MISSED + 1))
    [ "$MISSED" -lt 3 ] || er_die "no answer three times running. Incident $ID may still be alerting: acknowledge it on the phone, or run scripts/pause.sh"
    echo "t+$((i * 10))s: no answer, looking again"
    sleep 10
    continue
  fi
  MISSED=0
  STATE=$(printf '%s' "$REPLY_BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d["status"], "| calls:", d["attempts"], "| via:", d.get("acked_via"))
except (ValueError, KeyError, TypeError):
    sys.exit(1)') || {
    printf '%s\n' "$REPLY_BODY" >&2
    er_die "could not read incident $ID (HTTP $REPLY_STATUS); it may still be running"
  }
  echo "t+$((i * 10))s: $STATE"
  case "$STATE" in
    active*) sleep 10 ;;
    acknowledged*) exit 0 ;;
    # Expired or cancelled: the path may work, but nobody confirmed it.
    *) exit 1 ;;
  esac
done
echo "still active after 10 minutes" >&2; exit 1
