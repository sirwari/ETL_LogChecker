#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
PYTHON_BIN="${PYTHON_BIN:-}"

if [[ -x "$VENV_PYTHON" ]]; then
  PYTHON_CMD="$VENV_PYTHON"
elif [[ -n "$PYTHON_BIN" ]] && command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_CMD="$PYTHON_BIN"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
else
  echo "Python 3.11+ is required but was not found on PATH." >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  exec "$PYTHON_CMD" "$ROOT_DIR/etl_logchecker.py" --gui
fi

exec "$PYTHON_CMD" "$ROOT_DIR/etl_logchecker.py" "$@"
