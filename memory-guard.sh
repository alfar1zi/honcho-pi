#!/usr/bin/env bash
# memory-guard.sh — OMP memory guard: health check, backup, repair before reboot/shutdown.
# Reboot-safe: never fails hard, always exits 0 so systemd doesn't block shutdown.

set -uo pipefail

MEMORY_DIR="$HOME/.omp/memory"
DB="$MEMORY_DIR/memory.db"
BACKUP_DIR="$MEMORY_DIR/backups"
PYTHON="${PYTHON:-python3}"
LOG_FILE="$MEMORY_DIR/guard.log"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE" 2>/dev/null || true
}

MODE="${1:-backup-and-health}"

mkdir -p "$BACKUP_DIR" "$MEMORY_DIR/sessions"

run_python() {
  "$PYTHON" - "$DB" "$BACKUP_DIR" "$MODE" <<'PYEOF'
import sys, os, glob, shutil, json
from datetime import datetime, timezone

db = sys.argv[1]
backup_dir = sys.argv[2]
mode = sys.argv[3]

sys.path.insert(0, os.path.dirname(os.path.abspath(db)))
from memory_db import connect, init_db, integrity_check, quick_check, backup_db, stats, health

conn = connect(db)
init_db(conn)

# Health check
h = health(db)

if mode == "health":
    print(json.dumps(h, indent=2))
    conn.close()
    sys.exit(0)

if mode == "backup":
    bk = backup_db(db)
    print(f"Backup: {bk or 'skipped (no DB)'}")
    conn.close()
    sys.exit(0)

if mode == "backup-and-health":
    # Always backup first
    bk = backup_db(db)
    print(f"Backup: {bk or 'skipped (no DB)'}")

    # If corrupt, attempt repair
    if h["status"] != "ok":
        print(f"DB status: {h['status']} — attempting repair...")
        from memory_db import repair_from_backup
        if repair_from_backup(db):
            h2 = health(db)
            print(f"Repair: success — status={h2['status']}, records={h2['total_records']}")
        else:
            print("Repair: failed — DB may need manual intervention")
    else:
        print(f"DB status: ok — records={h['total_records']}")

    conn.close()
    sys.exit(0)

print(f"Unknown mode: {mode}")
conn.close()
sys.exit(1)
PYEOF
}

# Never fail hard — always exit 0
run_python || true
exit 0
