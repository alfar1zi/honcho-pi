# Session Capture

Capture lessons from omp sessions into the memory database.

## Security

- Disk space validation before capture (minimum 100MB free)
- Secret scanning: detects API keys, private keys, secret assignments
- JSON escaping via Python json.dumps (no injection)

## Deduplication

Checks for identical content already in DB before writing.
Skips duplicate records to keep the database clean.

## Session Lineage

- session_id + parent_id for context continuity across compressions
- Project detection from git remote URL
- Audit logging for all capture operations
