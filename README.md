# omp-memory

Persistent memory system for omp agents. SQLite + FTS5 full-text search.

## Architecture

```svg
<svg viewBox="0 0 700 420" xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="13">
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
      <polygon points="0 0, 10 3.5, 0 7" fill="var(--c1)"/>
    </marker>
  </defs>

  <!-- Capture -->
  <rect x="40" y="30" width="140" height="60" rx="6" fill="var(--surface)" stroke="var(--border)" stroke-width="1"/>
  <text x="110" y="52" text-anchor="middle" fill="var(--fg)">Capture Session</text>
  <text x="110" y="70" text-anchor="middle" fill="var(--muted)">capture-session.sh</text>

  <!-- Store -->
  <rect x="280" y="30" width="140" height="60" rx="6" fill="var(--surface)" stroke="var(--border)" stroke-width="1"/>
  <text x="350" y="52" text-anchor="middle" fill="var(--fg)">Memory DB</text>
  <text x="350" y="70" text-anchor="middle" fill="var(--muted)">SQLite + FTS5</text>

  <!-- Analyze -->
  <rect x="520" y="30" width="140" height="60" rx="6" fill="var(--surface)" stroke="var(--border)" stroke-width="1"/>
  <text x="590" y="52" text-anchor="middle" fill="var(--fg)">Analyze</text>
  <text x="590" y="70" text-anchor="middle" fill="var(--muted)">analyze-session.sh</text>

  <!-- Backup -->
  <rect x="280" y="130" width="140" height="60" rx="6" fill="var(--surface)" stroke="var(--border)" stroke-width="1"/>
  <text x="350" y="152" text-anchor="middle" fill="var(--fg)">Backup &amp; Guard</text>
  <text x="350" y="170" text-anchor="middle" fill="var(--muted)">memory-guard.sh</text>

  <!-- Arrows -->
  <line x1="180" y1="60" x2="280" y2="60" stroke="var(--c1)" stroke-width="2" marker-end="url(#arrow)"/>
  <line x1="350" y1="90" x2="520" y2="60" stroke="var(--c2)" stroke-width="2" marker-end="url(#arrow)"/>
  <line x1="350" y1="90" x2="350" y2="130" stroke="var(--c3)" stroke-width="2" marker-end="url(#arrow)"/>

  <!-- Labels -->
  <text x="230" y="50" text-anchor="middle" fill="var(--muted)" font-size="11">store</text>
  <text x="425" y="75" text-anchor="middle" fill="var(--muted)" font-size="11">analyze</text>
  <text x="350" y="125" text-anchor="middle" fill="var(--muted)" font-size="11">protect</text>
</svg>
```

## Components

| File | Purpose |
|------|---------|
| memory_db.py | SQLite + FTS5 core: WAL, integrity check, auto-backup, dedup |
| memory_provider.py | Abstract base class for pluggable memory providers |
| memory_context.py | Context fencing and output scrubbing |
| capture-session.sh | Capture session lessons with secret scanning |
| analyze-session.sh | Pattern detection and reinforcement |
| recap-service.sh | Session recap and summary |
| memory-guard.sh | Health check, backup, repair |
| disk-check.sh | Disk space validation |
| secret-scan.sh | Secret pattern detection |
| audit-log.sh | Audit logging |
| health-check.sh | System health verification |
| clean-code.sh | Clean code validation |
| install.sh | Installation script |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| OMP_MEMORY_DB | ~/.omp/memory/memory.db | Database path |
| OMP_MEMORY_BACKUP_DIR | ~/.omp/memory/backups | Backup directory |
| OMP_MEMORY_MAX_BACKUPS | 10 | Maximum backup count |
| OMP_MEMORY_BUSY_TIMEOUT_MS | 30000 | SQLite busy timeout |

## Quick Start

```bash
git clone https://github.com/alfar1zi/honcho-pi.git
cd honcho-pi
chmod +x *.sh
./install.sh
./health-check.sh
```

## License

MIT License.
