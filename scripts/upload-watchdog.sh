#!/bin/sh
# Monitor Filester upload speed and restart apu when it stays below threshold.
#
# Configure in .env:
#   UPLOAD_WATCHDOG_ENABLED=true
#   UPLOAD_WATCHDOG_MIN_MBPS=5
#   UPLOAD_WATCHDOG_SUSTAIN_SEC=60
#   UPLOAD_WATCHDOG_POLL_SEC=10
#   UPLOAD_WATCHDOG_COOLDOWN_SEC=300
#
# Run on the host (repo root), ideally under systemd or tmux:
#   ./scripts/upload-watchdog.sh
#   ./scripts/upload-watchdog.sh --dry-run
#
# One-shot check:
#   ./scripts/upload-watchdog.sh --once
set -e
cd "$(dirname "$0")/.."

DRY_RUN=0
ONCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --once)
      ONCE=1
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a
  . ./.env
  set +a
fi

enabled=$(printf '%s' "${UPLOAD_WATCHDOG_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')
case "$enabled" in
  true|1|yes|on) ;;
  *)
    echo "Upload watchdog disabled (UPLOAD_WATCHDOG_ENABLED=$enabled)"
    exit 0
    ;;
esac

POLL="${UPLOAD_WATCHDOG_POLL_SEC:-10}"
APU_PORT="${APU_PORT:-5000}"
BASE_URL="${UPLOAD_WATCHDOG_URL:-http://127.0.0.1:${APU_PORT}}"

run_check() {
  set +e
  qs=""
  if [ "$DRY_RUN" -eq 1 ]; then
    qs="?dry_run=1"
  fi
  body=$(curl -fsS "${BASE_URL}/api/upload_watchdog/check${qs}")
  curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    set -e
    return 1
  fi
  restart=$(printf '%s' "$body" | python3 -c 'import json,sys; print("1" if json.load(sys.stdin).get("restart_required") else "0")' 2>/dev/null || echo 0)
  if [ "$ONCE" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
    printf '%s\n' "$body" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$body"
  fi
  set -e
  if [ "$restart" = "1" ] && [ "$DRY_RUN" -eq 0 ]; then
    return 2
  fi
  return 0
}

if [ "$ONCE" -eq 1 ]; then
  if run_check; then
    exit 0
  else
    code=$?
    if [ "$code" -eq 2 ]; then
      echo "Restarting apu ..."
      docker compose restart apu
    fi
    exit "$code"
  fi
fi

echo "Upload watchdog running (poll every ${POLL}s, min ${UPLOAD_WATCHDOG_MIN_MBPS:-5} MB/s)"
while true; do
  if run_check; then
    :
  else
    code=$?
    if [ "$code" -eq 2 ]; then
      echo "$(date -Is) Upload watchdog: restarting apu ..."
      docker compose restart apu
      echo "$(date -Is) Waiting for container to come back ..."
      sleep 30
    elif [ "$code" -ne 2 ]; then
      echo "$(date -Is) Upload watchdog check failed (exit $code)" >&2
    fi
  fi
  sleep "$POLL"
done
