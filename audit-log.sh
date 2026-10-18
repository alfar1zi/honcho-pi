#!/usr/bin/env bash
# audit-log.sh - Audit logging for omp-memory operations.
# Usage: audit-log.sh <action> <session> <project>
set -uo pipefail

ACTION="${1:-}"
SESSION="${2:-}"
PROJECT="${3:-}"
MEMORY_DIR="$HOME/.omp/memory"
LOG_FILE="$MEMORY_DIR/audit.log"

mkdir -p "$MEMORY_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] action=$ACTION session=$SESSION project=$PROJECT" >> "$LOG_FILE" 2>/dev/null || true