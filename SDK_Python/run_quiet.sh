#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export HUNTER_LOG_MODE=off
exec bash "$ROOT/run.sh" "$@"
