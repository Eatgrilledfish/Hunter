#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -ne 1 ]]; then
  echo "usage: bash run.sh <port>" >&2
  exit 2
fi
exec "${HUNTER_PYTHON:-python3}" "$ROOT/CoreGeek/main3.py" "$1"
