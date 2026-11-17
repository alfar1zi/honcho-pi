#!/usr/bin/env bash
# audit-log.sh - append one audit line to the memory audit log.
# Usage: audit-log.sh <action> <detail> [session] [project]
set -uo pipefail

ACTION="${1:-}"
DETAIL="${2:-}"
SESSION="${3:-}"
PROJECT="${4:-}"
if [[ -z "$ACTION" ]]; then
  echo "usage: $0 <action> <detail> [session] [project]" >&2
  exit 2
fi

LOG_FILE="${OMP_MEMORY_DB:-$HOME/.omp/memory/memory.db}"
LOG_FILE="$(dirname "$LOG_FILE")/audit.log"
mkdir -p "$(dirname "$LOG_FILE")"
printf '[%s] action=%s session=%s project=%s detail=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$ACTION" "$SESSION" "$PROJECT" "$DETAIL" >> "$LOG_FILE"
