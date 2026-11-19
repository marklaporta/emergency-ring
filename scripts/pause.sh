#!/bin/bash
# The kill switch, from a machine with AWS access.
#
#   scripts/pause.sh          stop all alerting for 60 minutes
#   scripts/pause.sh 240      ... for 240 minutes (at most 1440)
#   scripts/pause.sh off      resume now
#
# Prints the backend's reply, and exits non-zero unless the backend said yes.
# Reads the endpoint from CloudFormation and a token from SSM, and does not
# print the token. Settings: ER_STACK, ER_REGION, ER_PREFIX and ER_SENDER, as
# in drill.sh (lib.sh has the details).
#
# The pause is recorded under the sender whose token is used, and the backend
# reports that name as paused_by: the first name in trigger-tokens unless
# ER_SENDER names another. This script reads the senders' list only. A token
# that can pause but cannot start an emergency is an entry in
# break-glass-tokens; a device that should hold nothing stronger can send
# POST /pause with one of those instead of running this script.
set -euo pipefail
ARG="${1:-60}"
usage() { echo "usage: $0 [minutes|off]" >&2; exit 2; }
# Checked before anything is looked up. Digits only, few enough for shell
# arithmetic, and not zero, which the backend would read as "the default".
case "$ARG" in
  off) ;;
  *[!0-9]*|??????*) usage ;;
  *) [ "$((10#$ARG))" -gt 0 ] || usage ;;
esac

case "$0" in */*) HERE="${0%/*}" ;; *) HERE=. ;; esac
. "$HERE/lib.sh"
er_lookup

if [ "$ARG" = "off" ]; then
  er_request 30 POST resume
else
  # 10# so that a leading zero is not read as octal, or sent as invalid JSON.
  er_request 30 POST pause "{\"minutes\": $((10#$ARG))}"
fi
printf '%s\n' "$REPLY_BODY"
er_ok || er_die "the backend answered HTTP $REPLY_STATUS, not success"
