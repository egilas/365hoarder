#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WORKER_DIR="$SCRIPT_DIR/workers"

if ! command -v python3 >/dev/null; then
  if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
    printf '\033[31mError:\033[0m python3 not found.\n' >&2
  else
    echo "Error: python3 not found." >&2
  fi
  exit 1
fi

exec python3 "$WORKER_DIR/sharepoint_enum.py" "$@"
