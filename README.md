# omp-memory

OMP Memory System — Persistent memory system for omp agents with SQLite + FTS5 full-text search.

## Overview

omp-memory is a persistent, cross-session memory system for AI agents. It stores session learnings, insights, and patterns in a SQLite database with FTS5 full-text search. Designed for per-user deployment (each user has their own `~/.omp/memory/` directory).

## Features

- **SQLite + FTS5** — Full-text search for fast memory retrieval
- **WAL journal mode** — Concurrent read/write safety
- **Automatic backup** — Rotating backups (max 10) with 0600 permissions
- **Auto-repair** — Corrupt database recovery from backups
- **Session lineage** — Parent/child session tracking for context continuity
- **Pre-compress checkpoints** — Fail-safe checkpoints before context compression
- **Profile isolation** — Per-user isolation via environment-based configuration
- **Secret scanning** — Automatic detection and blocking of API keys, passwords, tokens
- **Input sanitization** — Proper JSON escaping for all inputs
- **Deduplication** - Content-based deduplication to prevent duplicate records
- **Plugin system** - OpenCode plugin with auto-reload and fallback

## Quick Start

### Prerequisites

- Python 3.8+
- SQLite3 (usually pre-installed on Linux/macOS)
- Node.js (for the plugin)

### Installation

```bash
# Clone the repository
git clone https://github.com/alfar1zi/honcho-pi.git
cd honcho-pi

# Make scripts executable
chmod +x memory_db.py capture-session.sh analyze-session.sh recap-service.sh memory-guard.sh disk-check.sh secret-scan.sh audit-log.sh health-check.sh clean-code.sh

# Initialize the database
python3 memory_db.py

# Add to your shell profile (~/.bashrc, ~/.zshrc, etc.)
export OMP_MEMORY_DIR="$HOME/.omp/memory"
export OMP_MEMORY_DB="$HOME/.omp/memory/memory.db"
export PATH="$HOME/.omp/memory:$PATH"
```

## Usage

### Capture a lesson

```bash
capture-session.sh --task "Fix database connection" --outcome "Connection pool fixed with pool_size=10" --type FACT --verified 1 --confidence 0.9 --evidence "Tested under load for 1 hour" --tags "database,fix"
```

### Recall memory

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from memory_db import connect, init_db, search
conn = connect(); init_db(conn)
results = search(conn, 'database connection', limit=5)
for r in results:
    print(f\"{r['content']} (conf={r['confidence']})\")
conn.close()
"
```

### Analyze sessions

```bash
./analyze-session.sh analyze --project myproject
./analyze-session.sh promote --threshold 0.85 --project myproject
```

### Health check

```bash
./memory-guard.sh health
./memory-guard.sh backup-and-health
```

## Project Structure

```
honcho-pi/
├── memory_db.py          # SQLite + FTS5 memory database
├── memory_provider.py    # Hermes-inspired MemoryProvider ABC
├── memory_context.py     # Context fencing, StreamingContextScrubber
├── capture-session.sh    # Capture lessons from sessions
├── analyze-session.sh    # Analyze patterns and reinforce knowledge
├── recap-service.sh      # Auto-capture idle sessions, recap memory
├── memory-guard.sh       # Health check, backup, repair
├── disk-check.sh         # Disk space verification
├── secret-scan.sh        # Secret scanning (API keys, passwords, tokens)
├── audit-log.sh          | Audit logging for all operations
| health-check.sh         | Health check for all components
| clean-code.sh           | Code quality verification
| index.js                | OpenCode plugin
| memory_summary.md       | Primary memory summary
| .gitignore              | Git ignore rules
| LICENSE.md              | MIT license
| README.md               | This file
| INSTALL.md              | Installation guide
```

## Security

- All memory data is stored locally in `~/.omp/memory/`
- Backup files have 0600 permissions (owner read/write only)
- Secret scanning prevents API keys, passwords, and tokens from being stored
- Input validation on all inputs

## License

MIT License - see LICENSE.md for details.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Support

For issues and feature requests, please open an issue on GitHub.
