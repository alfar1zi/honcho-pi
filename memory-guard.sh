#!/usr/bin/env bash
# memory-guard.sh - OMP memory guard: health check, backup, repair.
set -uo pipefail

MEMORY_DIR="$HOME/.omp/memory"
DB="$MEMORY_DIR/memory.db"
PYTHON="${PYTHON:-python3}"

MODE="${1:-backup-and-health}"

"$PYTHON" - "$DB" "$MODE" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
from memory_db import connect, init_db, integrity_check, quick_check, backup_db, stats, health, repair_from_backup

db = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else 'backup-and-health'
conn = connect(db)
init_db(conn)
h = health(conn)
if mode == 'health':
    print(f"DB status: {h['status']}, records: {h['total_records']}, backups: {h['backup_count']}")
    conn.close()
    sys.exit(0 if h['status'] == 'ok' else 1)
if mode == 'backup':
    bk = backup_db(db)
    print(f"Backup: {bk or 'skipped (no DB)'}")
    conn.close()
    sys.exit(0)
if mode == 'backup-and-health':
    bk = backup_db(db)
    print(f"Backup: {bk or 'skipped (no DB)'}")
    print(f"DB status: {h['status']}, records: {h['total_records']}, backups: {h['backup_count']}")
    if h['status'] != 'ok':
        print('Attempting repair...')
        if repair_from_backup(db):
            h2 = health(db)
            print(f"Repair: success - status={h2['status']}, records={h2['total_records']}")
        else:
            print('Repair: failed - DB may need manual intervention')
            conn.close()
            sys.exit(1)
    conn.close()
    sys.exit(0)
print(f"Unknown mode: {mode}")
conn.close()
sys.exit(1)
PYEOF