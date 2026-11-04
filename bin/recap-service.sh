#!/usr/bin/env bash
# recap-service.sh - session recap / auto-capture shim over the omp-memory CLI.
# Subcommand "auto-capture" scans the latest session log for failure, file and
# decision patterns and writes them as low-confidence candidates, which is what
# the systemd timer expects; anything else passes through to "recap".
set -uo pipefail

VENV=/home/farizi/.omp/memory/venv
PY="$VENV/bin/python3"

if [[ "${1:-}" == "auto-capture" ]]; then
  shift
  exec "$PY" - << 'PYEOF' "$@"
import sys
from pathlib import Path
from omp_memory import db, capture

idle_minutes = 5
args = sys.argv[1:]
while args:
    if args[0] == "--idle-minutes" and len(args) > 1:
        idle_minutes = int(args[1]); args = args[2:]
    elif args[0] == "--project" and len(args) > 1:
        args = args[2:]
    else:
        args = args[1:]

session = capture.latest_session()
if session is None:
    sys.exit(0)
age_min = (time.time() - session.stat().st_mtime) / 60 if (time := __import__("time")) else 0
conn = db.connect(db.DB_PATH)
try:
    db.init_db(conn, db.DB_PATH)
    result = capture.capture_session(db, conn, session_path=session, apply=age_min <= idle_minutes * 3)
finally:
    conn.close()
print(f"auto-capture: {result.get('written', 0)} written from {result.get('session', '?')}")
PYEOF
fi

# recap passthrough
exec "$PY" -m omp_memory recap "$@"
