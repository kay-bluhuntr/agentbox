#!/usr/bin/env bash
# End-to-end smoke test against a running instance (default localhost:8080).
set -euo pipefail
HOST="${AGENTBOX_HOST:-http://localhost:8080}"

echo "--> submitting a session"
SESSION=$(curl -sf -X POST "${HOST}/v1/sessions" \
  -H 'Content-Type: application/json' \
  -d '{
        "image": "python:3.12-slim",
        "command": ["python", "-c", "import time; time.sleep(2); print(\"hello from the sandbox\")"],
        "timeout_seconds": 120,
        "max_retries": 1
      }')
ID=$(echo "$SESSION" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "    session: $ID"

echo "--> waiting for a terminal state"
for _ in $(seq 1 60); do
  STATE=$(curl -sf "${HOST}/v1/sessions/${ID}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["state"])')
  echo "    state: $STATE"
  case "$STATE" in
    succeeded) echo "--> logs:"; curl -sf "${HOST}/v1/sessions/${ID}/logs" | python3 -c 'import sys,json; print(json.load(sys.stdin)["logs"])'; exit 0 ;;
    failed|timed_out|cancelled) echo "smoke test FAILED (state=$STATE)"; exit 1 ;;
  esac
  sleep 2
done
echo "smoke test FAILED (timeout waiting for terminal state)"
exit 1
