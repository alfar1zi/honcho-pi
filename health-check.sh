#!/usr/bin/env bash
# health-check.sh — OMP memory health check.
# Runs on every session startup via load-global-context.
# Checks: DB integrity, backup existence, systemd timer status, systemd guard status.
#
# Usage: health-check.sh [--json]
#   Exits 0 if all checks pass, 1 if any check fails.
#
set -euo pipefail

MEMORY_DIR="$HOME/.omp/memory"
DB="$MEMORY_DIR/memory.db"
BACKUP_DIR="$MEMORY_DIR/backups"
PYTHON="${PYTHON:-python3}"

JSON_MODE=0
if [[ "${1:-}" == "--json" ]]; then
  JSON_MODE=1
fi

ERRORS=0
WARNINGS=0

log_msg() {
  if [[ $JSON_MODE -eq 1 ]]; then
    echo "$1"
  else
    echo "$1"
  fi
}

log_error() {
  ERRORS=$((ERRORS + 1))
  if [[ $JSON_MODE -eq 1 ]]; then
    echo "  \"$1\": \"ERROR\""
  else
    echo "  ✗ $1"
  fi
}

log_warn() {
  WARNINGS=$((WARNINGS + 1))
  if [[ $JSON_MODE -eq 1 ]]; then
    echo "  \"$1\": \"WARNING\""
  else
    echo "  ⚠ $1"
  fi
}

log_ok() {
  if [[ $JSON_MODE -eq 1 ]]; then
    echo "  \"$1\": \"OK\""
  else
    echo "  ✓ $1"
  fi
}

# Check 1: memory_db.py exists and is valid
if [[ -f "$MEMORY_DIR/memory_db.py" ]]; then
  if python3 -c "import sys; sys.path.insert(0, '$MEMORY_DIR'); import memory_db; print('OK')" 2>/dev/null; then
    log_ok "memory_db.py loads successfully"
  else
    log_error "memory_db.py fails to import"
  fi
else
  log_error "memory_db.py not found"
fi

# Check 2: memory.db exists and is healthy
if [[ -f "$DB" ]]; then
  HEALTH=$("$PYTHON" - "$DB" <<'PYEOF' 2>/dev/null || echo '{"status":"corrupt","total_records":0,"backup_count":0}'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
from memory_db import connect, init_db, health
conn = connect(sys.argv[1])
init_db(conn)
print(json.dumps(health(sys.argv[1])))
conn.close()
PYEOF
)
  DB_STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','unknown'))" 2>/dev/null || echo "unknown")
  DB_RECORDS=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total_records',0))" 2>/dev/null || echo "0")
  DB_BACKUPS=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('backup_count',0))" 2>/dev/null || echo "0")

  if [[ "$DB_STATUS" == "ok" ]]; then
    log_ok "Database healthy ($DB_RECORDS records, $DB_BACKUPS backups)"
  else
    log_error "Database unhealthy (status: $DB_STATUS)"
  fi
else
  log_error "memory.db not found"
fi

# Check 3: Backup directory exists and has backups
if [[ -d "$BACKUP_DIR" ]]; then
  BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/memory.db.bak.* 2>/dev/null | wc -l || echo "0")
  if [[ "$BACKUP_COUNT" -gt 0 ]]; then
    log_ok "Backup directory has $BACKUP_COUNT backup(s)"
  else
    log_warn "No backup files found"
  fi
else
  log_error "Backup directory not found"
fi

# Check 4: systemd timer status
if systemctl --user list-timers omp-memory-recap.timer >/dev/null 2>&1; then
  TIMER_STATE=$(systemctl --user is-active omp-memory-recap.timer 2>/dev/null || echo "inactive")
  if [[ "$TIMER_STATE" == "active" ]]; then
    log_ok "omp-memory-recap.timer is active"
  else
    log_error "omp-memory-recap.timer is $TIMER_STATE"
  fi
else
  log_error "omp-memory-recap.timer not found"
fi

# Check 5: systemd guard service status
if systemctl --user list-unit-files omp-memory-guard.service >/dev/null 2>&1; then
  GUARD_STATE=$(systemctl --user is-active omp-memory-guard.service 2>/dev/null || echo "inactive")
  if [[ "$GUARD_STATE" == "active" ]] || [[ "$GUARD_STATE" == "active (exited)" ]]; then
    log_ok "omp-memory-guard.service is active"
  else
    log_warn "omp-memory-guard.service is $GUARD_STATE"
  fi
else
  log_warn "omp-memory-guard.service not found"
fi

# Check 6: linger status
LINGER=$(loginctl show-user farizi -p Linger 2>/dev/null | cut -d= -f2 || echo "no")
if [[ "$LINGER" == "yes" ]]; then
  log_ok "loginctl linger is enabled"
else
  log_warn "loginctl linger is not enabled"
fi

# Summary
if [[ $JSON_MODE -eq 1 ]]; then
  echo "{"
  echo "  \"errors\": $ERRORS,"
  echo "  \"warnings\": $WARNINGS,"
  echo "  \"status\": \"$([ $ERRORS -eq 0 ] && echo 'ok' || echo 'degraded')\""
  echo "}"
else
  echo ""
  echo "========================================"
  if [[ $ERRORS -eq 0 && $WARNINGS -eq 0 ]]; then
    echo "  ALL CHECKS PASSED"
  elif [[ $ERRORS -eq 0 ]]; then
    echo "  ALL CHECKS PASSED ($WARNINGS warning(s))"
  else
    echo "  $ERRORS ERROR(S), $WARNINGS WARNING(S)"
  fi
  echo "========================================"
fi

exit $ERRORS
