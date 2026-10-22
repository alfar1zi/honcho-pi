# Memory Core

SQLite-backed memory system with FTS5 full-text search for omp agents.

## Core Module: memory_db.py

- SQLite WAL journal mode for concurrent read/write access
- FTS5 virtual table for full-text search on records
- Content-based deduplication via SHA-256 record IDs
- Automatic WAL backup rotation (max 10 backups)
- PRAGMA integrity_check on every connection open
- Busy timeout for concurrent access protection

## Record Types

FACT, ASSUMPTION, INFERENCE, DECISION, RULE, CORRECTION

## Reinforcement Signals

success, failure, correction, reinforce, decay - each adjusts record confidence.
