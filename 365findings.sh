#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WORKER_DIR="$SCRIPT_DIR/workers"

if ! command -v python3 >/dev/null; then
  printf 'Error: python3 not found.\n' >&2
  exit 1
fi

exec python3 "$WORKER_DIR/m365_findings.py" "$@"
