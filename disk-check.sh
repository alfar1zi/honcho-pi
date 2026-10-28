#!/usr/bin/env bash
# disk-check.sh — Check disk space before writing to memory DB.
# Exits 0 if enough space, 1 if not enough.
#
# Usage: disk-check.sh [min_free_mb]
#   Default min_free_mb: 100

set -euo pipefail

MEMORY_DIR="$HOME/.omp/memory"
MIN_FREE_MB="${1:-100}"

# Get available space in KB for the memory directory
AVAILABLE_KB=$(df -k "$MEMORY_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
if [[ -z "$AVAILABLE_KB" ]]; then
    echo "ERROR: Cannot determine disk space for $MEMORY_DIR" >&2
    exit 1
fi

AVAILABLE_MB=$((AVAILABLE_KB / 1024))

if [[ $AVAILABLE_MB -lt $MIN_FREE_MB ]]; then
    echo "ERROR: Insufficient disk space: ${AVAILABLE_MB}MB available, ${MIN_FREE_MB}MB required" >&2
    exit 1
fi

echo "OK: ${AVAILABLE_MB}MB available (${MIN_FREE_MB}MB required)"
exit 0
