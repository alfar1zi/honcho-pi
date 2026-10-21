#!/usr/bin/env bash
# disk-check.sh - Check available disk space (minimum free MB required).
# Usage: disk-check.sh <min_free_mb> [--json]
set -uo pipefail

MIN_FREE_MB="${1:-100}"
JSON="${2:-}"

DF_OUTPUT=$(df -m "$HOME/.omp/memory" 2>/dev/null | tail -1 || df -m . | tail -1)
AVAILABLE=$(echo "$DF_OUTPUT" | awk '{print $4}')

if [[ -z "$AVAILABLE" || "$AVAILABLE" =~ ^[^0-9] ]]; then
    if [[ "$JSON" == "--json" ]]; then
        echo '{"available_mb": 0, "min_required_mb": '$MIN_FREE_MB', "ok": false}'
    else
        echo "ERROR: Cannot determine available disk space"
        exit 1
    fi
fi

OK=1
if [[ "$AVAILABLE" -lt "$MIN_FREE_MB" ]]; then
    OK=0
fi

if [[ "$JSON" == "--json" ]]; then
    echo "{\"available_mb\": $AVAILABLE, \"min_required_mb\": $MIN_FREE_MB, \"ok\": $OK}"
else
    if [[ $OK -eq 1 ]]; then
        echo "OK: ${AVAILABLE}MB free (need ${MIN_FREE_MB}MB)"
        exit 0
    else
        echo "WARNING: Only ${AVAILABLE}MB free (need ${MIN_FREE_MB}MB)"
        exit 1
    fi
fi
