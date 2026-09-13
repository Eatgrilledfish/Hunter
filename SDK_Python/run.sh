#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODE=compact
if [[ -f "$ROOT/log_mode.conf" ]]; then
  MODE=$(<"$ROOT/log_mode.conf")
fi
export HUNTER_LOG_MODE="${HUNTER_LOG_MODE:-$MODE}"
case "$HUNTER_LOG_MODE" in
  off) exec >/dev/null 2>&1 ;;
  compact|full) ;;
  *) echo "invalid HUNTER_LOG_MODE: use off, compact or full" >&2; exit 2 ;;
esac
if [[ $# -ne 1 ]]; then
  echo "usage: bash run.sh <port>" >&2
  exit 2
fi
exec "${HUNTER_PYTHON:-python3}" "$ROOT/CoreGeek/main3.py" "$1"
