#!/usr/bin/env bash
# Run the bench harness under every available interpreter, one JSON per version.
# Missing interpreters are recorded, not fatal: the point is a per-version table.
set -u

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$BENCH_DIR/out"
mkdir -p "$OUT_DIR"

# Each run gets its own DB so parallel/leftover state cannot leak between versions.
status=0
for ver in 3.10 3.12 3.13; do
    name="python$ver"
    json="$OUT_DIR/run_$ver.json"
    log="$OUT_DIR/run_$ver.err"
    if ! command -v "$name" >/dev/null 2>&1; then
        printf '%s\n' "TIDAK ADA: $name not found on PATH" > "$json"
        printf '%-10s TIDAK ADA\n' "$name" >&2
        continue
    fi
    OMP_MEMORY_DB="$BENCH_DIR/db/bench_$ver.db" \
        "$name" "$BENCH_DIR/harness.py" > "$json" 2> "$log"
    rc=$?
    if [ $rc -ne 0 ]; then
        printf '%-10s FAIL rc=%d (see %s)\n' "$name" "$rc" "$log" >&2
        status=1
        continue
    fi
    printf '%-10s ok -> %s\n' "$name" "$(basename "$json")" >&2
done

exit $status
