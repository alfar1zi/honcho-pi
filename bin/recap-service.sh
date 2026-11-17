#!/usr/bin/env bash
# recap-service.sh - print a session or project recap
# Thin shim over the omp-memory CLI; behaviour lives in src/omp_memory/cli.py.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
if [[ -d "$ROOT_DIR/src/omp_memory" ]]; then
  export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
fi

exec "$PYTHON" -m omp_memory recap "$@"
