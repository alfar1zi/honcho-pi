#!/usr/bin/env bash
# install.sh - Install omp-memory system.
set -euo pipefail

MEMORY_DIR="$HOME/.omp/memory"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing omp-memory to $MEMORY_DIR..."
mkdir -p "$MEMORY_DIR"

cp "$SCRIPT_DIR/memory_db.py" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/memory_provider.py" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/memory_context.py" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/capture-session.sh" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/analyze-session.sh" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/recap-service.sh" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/memory-guard.sh" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/disk-check.sh" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/secret-scan.sh" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/audit-log.sh" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/health-check.sh" "$MEMORY_DIR/"
cp "$SCRIPT_DIR/clean-code.sh" "$MEMORY_DIR/"
chmod +x "$MEMORY_DIR"/*.sh

echo "Installation complete!"
echo "Memory DB: $MEMORY_DIR/memory.db"
echo "Run: $MEMORY_DIR/health-check.sh"