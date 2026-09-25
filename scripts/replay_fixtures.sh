#!/usr/bin/env sh
# POST the recorded Vapi webhook fixtures to a running server and print the responses.
#   VAPI_WEBHOOK_SECRET=... scripts/replay_fixtures.sh [http://localhost:8000]
set -eu
BASE="${1:-http://localhost:8000}"
DIR="$(dirname "$0")/../tests/fixtures"
for f in assistant_request tool_calls_weather tool_calls_search tool_calls_followup status_update_ended end_of_call_report; do
  echo "== $f"
  curl -sS -X POST "$BASE/vapi/webhook" \
    -H "Content-Type: application/json" \
    -H "X-Vapi-Secret: ${VAPI_WEBHOOK_SECRET:?set VAPI_WEBHOOK_SECRET}" \
    --data @"$DIR/$f.json" | head -c 600
  echo
done
