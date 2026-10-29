#!/usr/bin/env bash
# memory-guard.sh - backup, health, and repair for the memory DB.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
if [[ -d "$ROOT_DIR/src/omp_memory" ]]; then
  export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
fi

MODE="${1:-backup-and-health}"
case "$MODE" in
  backup)  exec "$PYTHON" -m omp_memory backup ;;
  health)  exec "$PYTHON" -m omp_memory health ;;
  repair)  exec "$PYTHON" -m omp_memory repair ;;
  backup-and-health)
    "$PYTHON" -m omp_memory backup || exit 1
    exec "$PYTHON" -m omp_memory health
    ;;
  *)
    echo "usage: $0 [backup|health|repair|backup-and-health]" >&2
    exit 2
    ;;
esac
