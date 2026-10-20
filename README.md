# honcho-pi

Persistent memory for coding agents, stored in one SQLite file on your machine.

Your agent forgets every lesson at the end of a session. It re-learns the same
build command, re-makes the same wrong assumption, and asks you to explain the
same constraint again next week. honcho-pi fixes that with a local database and
a searchable memory the agent can read at the start of a turn.

No server. No API key. No embeddings bill. One file you can back up with `cp`.

```bash
npx skills add alfar1zi/honcho-pi
```

## Why it exists

Three failure modes kill agent memory in practice:

1. **Memory that lies.** A note written once and never re-checked becomes
   confidently wrong. honcho-pi stores `verified`, `confidence`, and a
   reinforcement history per record, so a lesson that failed twice drops below
   the promotion threshold instead of hardening into a rule.
2. **Memory that vanishes.** Compression, crashes, and context resets erase what
   the agent just learned. Capture writes an append-only JSONL log before it
   touches the database, and a pre-compress checkpoint stores the full record
   payload for restore.
3. **Memory that corrupts.** A raw `cp` of a live SQLite database captures a
   torn state, and an "auto-repair" that deletes the file on open failure
   destroys the only copy. honcho-pi takes consistent `VACUUM INTO` snapshots,
   never deletes a database it failed to open, and repairs only from a backup
   that passes `quick_check`.

## Install

Requires Python 3.9+ with the `sqlite3` module (standard library).

```bash
# as an agent skill
npx skills add alfar1zi/honcho-pi

# or as a python package
pip install git+https://github.com/alfar1zi/honcho-pi
```

Manual install into `~/.omp/memory`:

```bash
git clone https://github.com/alfar1zi/honcho-pi
cd honcho-pi
bash install.sh
source ~/.omp/memory/env.sh
omp-memory health
```

`install.sh` is idempotent. Re-running it replaces the code and leaves your
database, backups, capture logs, and audit log alone. Use `--dry-run` to preview.

## Use

```bash
# record what happened, and whether it worked
omp-memory capture --task "fix WAL corruption" --outcome "set busy_timeout" --verified

# search it back, any time later
omp-memory search "wal corruption"

# what did this session actually do
omp-memory recap --session sess-20261029T101500Z

# a fenced block ready to inject into a prompt
omp-memory context "sqlite backup"

# mark the lesson as good or bad
omp-memory reinforce <record_id> success

# verified + confident inferences become rules
omp-memory promote --threshold 0.85

# snapshot the evidence before a context compression
omp-memory checkpoint --session sess-20261029T101500Z
```

Every command supports `--json` where the output is structured, so an agent can
parse it without scraping.

## How it works

```
capture ──> sessions + records ──> records_fts (FTS5, trigger-synced)
   │                │
   │                └──> reinforcement (append-only signal log)
   └──> sessions/*.jsonl (append-only, written before the DB)

records ──> checkpoint payload (JSON) ──> restore after compression
```

- **Records** carry `type` (FACT, ASSUMPTION, INFERENCE, DECISION, RULE,
  CORRECTION), `evidence`, `verified`, `confidence`, `source`, `tags`,
  `session_id`, `project_id`, and `parent_id`.
- **Identity is content-addressed.** `sha256(type:content:source:project)` means
  the same lesson captured twice updates one row instead of piling up
  duplicates. Different projects stay separate.
- **FTS5 is maintained by triggers**, so any write path keeps the index honest.
  Search input is tokenised and quoted, so a query containing `"`, `*`, `-`, or
  `NEAR(` is searched for, not executed as an operator.
- **Confidence is clamped to [0, 1]** and every change is recorded in
  `reinforcement` with the signal, delta, source, and timestamp.
- **Sessions** are a tree. `parent_id` records lineage across a compression or a
  delegated sub-session, so a recap can walk back to the parent context.
- **Profiles** are isolated through `profile_contexts`, keyed by
  `(profile, key)`, with no module-level mutable state.

## Reliability

| Concern | Behaviour |
|---|---|
| Concurrent writers | `journal_mode=WAL`, `busy_timeout=30000` (configurable) |
| Backup | `VACUUM INTO`, consistent snapshot, 0600, rotated to `OMP_MEMORY_MAX_BACKUPS` |
| Corrupt database | `quick_check` then `integrity_check`; open failure raises and leaves the file in place |
| Repair | newest backup that passes `quick_check`, restored via atomic `os.replace` |
| Capture durability | JSONL appended before the DB write |
| Secret leakage | scan runs before store; `--allow-secrets` is an explicit opt-in |
| Disk exhaustion | capture refuses below `--min-free-mb` (default 100) |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OMP_MEMORY_DB` | `~/.omp/memory/memory.db` | database file |
| `OMP_MEMORY_BACKUP_DIR` | `~/.omp/memory/backups` | snapshot directory |
| `OMP_MEMORY_MAX_BACKUPS` | `10` | snapshots kept |
| `OMP_MEMORY_BUSY_TIMEOUT_MS` | `30000` | SQLite lock wait |
| `OMP_MEMORY_HOME` | `~/.omp/memory` | install target for `install.sh` |

## Agent integration

The package ships a `MemoryProvider` base class and a context scrubber so an
agent runtime can wire recall in without importing internals:

```python
from omp_memory import MemoryProvider, build_memory_context_block, sanitize_context

class MyMemory(MemoryProvider):
    @property
    def name(self) -> str:
        return "honcho-pi"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self.session_id = session_id

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # return build_memory_context_block(...) from your search results
        return ""
```

`sanitize_context` and `StreamingContextScrubber` strip the fence from anything
that leaves the agent, including when a fence is split across streaming deltas.

## Development

```bash
python -m pytest          # 79 tests, no network, no shared state
bash bin/clean-code.sh    # secrets, eval, markers, em dashes, line length
bash install.sh --dry-run # preview an install
```

Tests use a per-test temporary database. Nothing touches your real memory.

## License

MIT. See [LICENSE.md](LICENSE.md).
