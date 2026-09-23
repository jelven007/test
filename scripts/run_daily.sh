#!/bin/sh
set -eu

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Missing virtual environment: $PYTHON_BIN" >&2
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

mkdir -p "$PROJECT_ROOT/logs" "$PROJECT_ROOT/reports"
export PYTHONPATH="$PROJECT_ROOT/src"

cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" -m banxia_strategy run \
  --config "$PROJECT_ROOT/config/strategy.json" \
  --output "$PROJECT_ROOT/reports"
