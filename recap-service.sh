#!/usr/bin/env bash
# recap-service.sh - Recap service for omp-memory system.
# Provides session recap and summary generation.
set -uo pipefail

MEMORY_DIR="$HOME/.omp/memory"
DB="$MEMORY_DIR/memory.db"
PYTHON="${PYTHON:-python3}"

SESSION="${1:-}"

if [[ -z "$SESSION" ]]; then
  echo "Usage: $0 <session_id>" >&2
  exit 2
fi

"$PYTHON" - "$DB" "$SESSION" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
from memory_db import connect, init_db, list_records

db = sys.argv[1]
session_id = sys.argv[2]
conn = connect(db)
init_db(conn)
records = [r for r in list_records(conn) if r.get('session_id') == session_id]
print(f"Session: {session_id}")
print(f"Records: {len(records)}")
for r in records:
    print(f"  - [{r['type']}] {r['content'][:80]} (confidence={r['confidence']})")
conn.close()
PYEOF