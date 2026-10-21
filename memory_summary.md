# omp-memory System Summary

## Overview
Persistent, cross-session memory system for omp agents with SQLite + FTS5 full-text search.
Inspired by Hermes Agent (NousResearch/hermes-agent).

## Architecture
- SQLite-backed with WAL journal mode and busy_timeout for concurrent access
- FTS5 full-text search for fast semantic-style queries
- Session lineage (parent/child) for context continuity across compressions
- Pre-compress checkpoint v2 (fail-closed evidence handoff)
- Profile isolation — no module/class-level mutable state
- Automatic backup before every write (rotation, max 10)
- integrity_check on every open with auto-repair from backup
- Content-based dedup via SHA-256 record IDs

## Record Types
FACT, ASSUMPTION, INFERENCE, DECISION, RULE, CORRECTION

## Reinforcement Signals
success, failure, correction, reinforce, decay

## Files
- memory_db.py — Core SQLite database module
- memory_provider.py — Abstract base class for pluggable providers
- memory_context.py — Memory context fencing
- capture-session.sh — Session capture script
- analyze-session.sh — Analysis session script
- recap-service.sh — Recap service
- memory-guard.sh — Memory guard
- disk-check.sh — Disk space check
- secret-scan.sh — Secret scanning
- audit-log.sh — Audit logging
- health-check.sh — Health check
- clean-code.sh — Clean code validation

## Configuration
OMP_MEMORY_DB — database path (default: ~/.omp/memory/memory.db)
OMP_MEMORY_BACKUP_DIR — backup directory (default: ~/.omp/memory/backups)
OMP_MEMORY_MAX_BACKUPS — max backup count (default: 10)
OMP_MEMORY_BUSY_TIMEOUT_MS — SQLite busy timeout (default: 30000)