#!/usr/bin/env bash
# audit-log.sh — OMP memory audit log.
# Logs all memory operations (capture, analyze, promote, reinforce, delete).
#
# Usage: audit-log.sh <action> <details>
#   action: capture, analyze, promote, reinforce, delete, session_start, session_end
#   details: free-text description of what happened
#
set -euo pipefail

MEMORY_DIR="$HOME/.omp/memory"
AUDIT_LOG="$MEMORY_DIR/audit.log"
PYTHON="${PYTHON:-python3}"

ACTION="${1:-}"
DETAILS="${2:-}"

if [[ -z "$ACTION" ]]; then
  echo "Usage: $0 <action> <details>" >&2
  exit 1
fi

mkdir -p "$MEMORY_DIR"

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SESSION_ID="${OMP_SESSION_ID:-unknown}"
PROJECT_ID="${OMP_PROJECT_ID:-unknown}"

echo "[$TIMESTAMP] session=$SESSION_ID project=$PROJECT_ID action=$ACTION details=$DETAILS" >> "$AUDIT_LOG"

# Also log to memory_db for querying
"$PYTHON" - "$MEMORY_DIR/memory.db" "$ACTION" "$DETAILS" "$SESSION_ID" "$PROJECT_ID" "$TIMESTAMP" <<'PYEOF' 2>/dev/null || true
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
from memory_db import connect, init_db, upsert_record
db = sys.argv[1]
action = sys.argv[2]
details = sys.argv[3]
session_id = sys.argv[4]
project_id = sys.argv[5]
timestamp = sys.argv[6]
conn = connect(db)
init_db(conn)
content = f"audit: {action} - {details}"
conn.execute("""
    INSERT INTO records (id, type, content, evidence, verified, confidence, source, tags, created_at, updated_at, session_id, project_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    __import__('hashlib').sha256(f"{action}:{details}:{timestamp}".encode()).hexdigest()[:16],
    "DECISION",
    content,
    details,
    1,  # verified
    0.95,
    "audit-log",
    "audit",
    timestamp,
    timestamp,
    session_id,
    project_id,
))
conn.commit()
conn.close()
PYEOF

echo "Audit logged: $ACTION - $DETAILS"
