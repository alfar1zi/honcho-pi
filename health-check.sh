#!/usr/bin/env bash
# health-check.sh — Health check for omp-memory system.
set -uo pipefail

MEMORY_DIR="$HOME/.omp/memory"
DB="$MEMORY_DIR/memory.db"
PYTHON="${PYTHON:-python3}"

"$PYTHON" - "$DB" health <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
from memory_db import connect, init_db, health
conn = connect(sys.argv[1])
init_db(conn)
h = health(conn)
print(f"Status: {h['status']}")
print(f"DB: {h['db_path']}")
print(f"Backups: {h['backup_count']}")
print(f"Records: {h['total_records']}")
conn.close()
sys.exit(0 if h['status'] == 'ok' else 1)
PYEOF