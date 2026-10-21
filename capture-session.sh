#!/usr/bin/env bash
# capture-session.sh — capture lessons from the current omp session into memory DB.
# Reboot-safe: never fails hard, creates DB if missing, graceful degrade on error.
# Supports session lineage (parent_id) for context continuity across compressions.
# Security: disk space check, secret scanning, JSON escaping, dedup.

set -uo pipefail

MEMORY_DIR="$HOME/.omp/memory"
DB="$MEMORY_DIR/memory.db"
SESSIONS_DIR="$MEMORY_DIR/sessions"
PYTHON="${PYTHON:-python3}"

TASK=""
OUTCOME=""
TYPE="FACT"
EVIDENCE=""
VERIFIED=0
CONFIDENCE=0.5
SOURCE="session-capture"
TAGS=""
PROJECT=""
SESSION=""
PARENT_ID=""

cleanup() { :; }
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task)       TASK="$2"; shift 2 ;;
    --outcome)    OUTCOME="$2"; shift 2 ;;
    --type)       TYPE="$2"; shift 2 ;;
    --evidence)   EVIDENCE="$2"; shift 2 ;;
    --verified)   VERIFIED="$2"; shift 2 ;;
    --confidence) CONFIDENCE="$2"; shift 2 ;;
    --source)     SOURCE="$2"; shift 2 ;;
    --tags)       TAGS="$2"; shift 2 ;;
    --project)    PROJECT="$2"; shift 2 ;;
    --session)    SESSION="$2"; shift 2 ;;
    --parent-id)  PARENT_ID="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$TASK" || -z "$OUTCOME" ]]; then
  echo "Usage: $0 --task '...' --outcome '...' [--type FACT] [--evidence '...'] [--verified 1] [--parent-id ...]" >&2
  exit 2
fi

# --- Disk space check ---
if ! "$MEMORY_DIR/disk-check.sh" 100; then
    echo "ERROR: Insufficient disk space. Capture aborted." >&2
    exit 1
fi

# --- Secret scanning ---
COMBINED_TEXT="${TASK} ${OUTCOME} ${EVIDENCE}"
SCAN_OUTPUT=$("$MEMORY_DIR/secret-scan.sh" "$COMBINED_TEXT" 2>&1) || RC=$? || true
if [[ -n "$SCAN_OUTPUT" ]]; then
    echo "WARNING: Potential secrets detected in input. Capture aborted for security." >&2
    echo "$SCAN_OUTPUT" >&2
    exit 1
fi

mkdir -p "$SESSIONS_DIR"

TS=$(date -u +%Y%m%dT%H%M%SZ)
CAPTURE_FILE="$SESSIONS_DIR/capture-$TS.jsonl"

# Detect project from git remote if available
if [[ -z "$PROJECT" ]] && git rev-parse --git-dir >/dev/null 2>&1; then
  REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
  if [[ -n "$REMOTE" ]]; then
    PROJECT=$(echo -n "$REMOTE" | sha256sum | cut -c1-12)
  else
    PROJECT=$(git rev-parse --show-toplevel 2>/dev/null | sha256sum | cut -c1-12)
  fi
fi

if [[ -z "$SESSION" ]]; then
  SESSION="sess-$TS"
fi

# Build JSON payload using Python for proper escaping
PAYLOAD=$(python3 -c "
import json, sys
record = {
    'timestamp': sys.argv[1],
    'session': sys.argv[2],
    'project': sys.argv[3],
    'task': sys.argv[4],
    'outcome': sys.argv[5],
    'type': sys.argv[6],
    'evidence': sys.argv[7],
    'verified': int(sys.argv[8]),
    'confidence': float(sys.argv[9]),
    'source': sys.argv[10],
    'tags': sys.argv[11],
    'parent_id': sys.argv[12]
}
print(json.dumps(record, ensure_ascii=False))
" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SESSION" "$PROJECT" "$TASK" "$OUTCOME" "$TYPE" "$EVIDENCE" "$VERIFIED" "$CONFIDENCE" "$SOURCE" "$TAGS" "$PARENT_ID")

# Append JSONL first (always works even if DB fails)
echo "$PAYLOAD" >> "$CAPTURE_FILE"

# Dedup check: skip if identical content already exists in DB
EXISTING_ID=$("$PYTHON" - "$DB" "$TASK" "$OUTCOME" "$TYPE" <<'PYEOF' 2>/dev/null || true
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
from memory_db import connect, init_db, list_records
db = sys.argv[1]
target_task = sys.argv[2]
target_outcome = sys.argv[3]
target_type = sys.argv[4]
conn = connect(db)
init_db(conn)
records = list_records(conn)
for r in records:
    if (r['type'] == target_type and
        r['content'] == f"{target_task} -> {target_outcome}"):
        print(r['id'])
        sys.exit(0)
conn.close()
PYEOF
)

if [[ -n "$EXISTING_ID" ]]; then
    echo "DUPLICATE: Record already exists ($EXISTING_ID). Capture skipped."
    exit 0
fi

# Upsert into memory.db via Python
"$PYTHON" - "$DB" "$CAPTURE_FILE" "$PAYLOAD" <<'PYEOF' || echo "WARNING: capture write to DB failed (DB may be corrupt or locked), JSONL saved" >&2
import json, sys, os

capture_file = sys.argv[1]
db = sys.argv[2]
payload = sys.argv[3]

mem_dir = os.path.dirname(os.path.abspath(db))
sys.path.insert(0, mem_dir)
from memory_db import connect, init_db, upsert_record

record = json.loads(payload)
content = f"{record['task']} -> {record['outcome']}"

conn = connect(db)
init_db(conn)
rid = upsert_record(
    conn,
    rtype=record.get("type", "FACT"),
    content=content,
    evidence=record.get("evidence", ""),
    verified=int(record.get("verified", 0)),
    confidence=float(record.get("confidence", 0.5)),
    source=record.get("source", "session-capture"),
    tags=record.get("tags", ""),
    session_id=record.get("session", ""),
    project_id=record.get("project", ""),
    parent_id=record.get("parent_id", ""),
)
print(f"Upserted memory record: {rid} (type={record.get('type','FACT')}, verified={record.get('verified',0)}, confidence={record.get('confidence',0.5)})")
conn.close()

print(f"Capture saved: {capture_file}")
PYEOF

# Audit log
"$MEMORY_DIR/audit-log.sh" capture "$TASK -> $OUTCOME" "$SESSION" "$PROJECT" 2>/dev/null || true

echo "Capture session: $SESSION"
