#!/usr/bin/env bash
# analyze-session.sh - analyze captures, reinforce, or promote records.
# Thin shim: `stats`, `reinforce`, `promote`, `analyze` subcommands.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
if [[ -d "$ROOT_DIR/src/omp_memory" ]]; then
  export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
fi

MODE="${1:-}"
case "$MODE" in
  stats)     shift; exec "$PYTHON" -m omp_memory stats "$@" ;;
  analyze)   shift; exec "$PYTHON" -m omp_memory analyze "$@" ;;
  promote)   shift; exec "$PYTHON" -m omp_memory promote "$@" ;;
  reinforce)
    shift
    RID="${1:-}"; SIGNAL="${2:-}"
    if [[ -z "$RID" || -z "$SIGNAL" ]]; then
      echo "usage: $0 reinforce <record_id> <signal> [--delta D] [--source S]" >&2
      exit 2
    fi
    shift 2
    exec "$PYTHON" -m omp_memory reinforce "$RID" "$SIGNAL" "$@"
    ;;
  ""|-h|--help)
    cat <<'USAGE'
usage: analyze-session.sh <command> [options]

  stats                          record counts by type
  analyze [--project P]          derive INFERENCE rows from capture logs
  promote [--threshold 0.85]     promote verified records to RULE
  reinforce <id> <signal>        apply success|failure|correction|reinforce|decay
USAGE
    exit 0
    ;;
  *)
    echo "error: unknown command '$MODE'" >&2
    exit 2
    ;;
esac
