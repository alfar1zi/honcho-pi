# honcho-pi

Local memory for coding agents: one SQLite file that stores what worked, so the next session starts where the last
one stopped.

```bash
npx skills add alfar1zi/honcho-pi
```

That command installs honcho-pi as an agent skill. There is no server, no API key, and no embedding model. The
database is one file you can copy with `cp`.

## What it does

An agent finishes a task and stores the lesson:

```python
m.remember(conn, "FACT", "Install dependencies with uv, not pip", verified=1, source="s1")
# {"id": "7b087cacbcd14536", "applied": "inserted", "action": "create", "reason": "novel", ...}
```

A later session writes the same lesson with different casing and a new source:

```python
m.remember(conn, "FACT", "install dependencies with UV, not pip", source="s2")
# same id, "applied": "updated", "reason": "identical", source becomes "s1,s2"
```

When a new turn needs context, recall ranks the stored rows and returns the best ones:

```python
m.recall(conn, "install dependencies", limit=2)
# [{"id": "7b087cacbcd14536", "type": "FACT", "confidence": 0.5, "verified": 1,
#   "score": 0.81875, "heat": 0.475, "snippet": "install dependencies with UV, not pip"}]
```

All three calls ran against an empty temporary database, and the output above is what they printed.

## Contents

- [What it does](#what-it-does)
- [Why it exists](#why-it-exists)
- [Install](#install)
- [CLI](#cli)
- [How it works](#how-it-works)
- [Recall](#recall)
- [Mirror](#mirror)
- [Reliability](#reliability)
- [Configuration](#configuration)
- [Agent integration](#agent-integration)
- [MCP server](#mcp-server)
- [Development](#development)
- [Limits](#limits)
- [License](#license)

## Why it exists

An agent forgets every lesson at session end. It re-learns the same build command and repeats the same wrong
assumption next week. Three failures follow from that:

1. **Memory that lies.** A note written once and never re-checked becomes confidently wrong. Each record tracks
   `verified`, `confidence`, and an append-only signal log, so a lesson that fails loses ground instead of hardening.
2. **Memory that vanishes.** Compression and context resets erase recent lessons. Capture commits the DB row first,
   then appends a line to a JSONL file. Checkpoint and restore move a session payload through a compression.
3. **Memory that corrupts.** A raw copy of a live database can capture a half-written state. Some repair tools delete
   the file when it fails to open. honcho-pi snapshots with `VACUUM INTO` and never deletes a database it cannot open.

## Install

Requires Python 3.10+ with the standard `sqlite3` module.

```bash
# as an agent skill
npx skills add alfar1zi/honcho-pi

# or as a python package
pip install git+https://github.com/alfar1zi/honcho-pi
```

### Quick setup

One command, no clone, no `cd`. `pipx` installs the CLI into its own environment, so it works the same on Linux,
macOS, and Windows and never trips the "externally managed environment" block that plain `pip` hits on Debian and
Ubuntu. The database defaults to `~/.omp/memory/memory.db`; set `OMP_MEMORY_DB` to move it.

```bash
pipx install "git+https://github.com/alfar1zi/honcho-pi"
pipx ensurepath    # only if the shell cannot find `omp-memory` afterwards; reopen the shell
```

No `pipx`? Install it first (`apt install pipx`, `brew install pipx`, or the `pip install --user pipx` line for
your interpreter), or fall back to a venv. Use `python3` on Linux, `python` on macOS and Windows:

```bash
python3 -m venv ~/.local/venvs/omp-memory
~/.local/venvs/omp-memory/bin/python -m pip install "git+https://github.com/alfar1zi/honcho-pi"
```

Verify either install:

```bash
omp-memory health                          # pipx install
~/.local/venvs/omp-memory/bin/omp-memory health   # venv install
```

The `omp-memory` console script and `python -m omp_memory` call the same `main()`. On Windows the venv path ends in
`\Scripts\python.exe` instead of `bin/python`. The `bin/*` scripts and `install.sh` are bash shims for POSIX
installs only.

Paste this block into an agent to make it use the memory:

```text
You have a local memory CLI. Use it without being asked.
- `omp-memory` is on PATH after a pipx install; `python -m omp_memory` is the same program if the shim is missing.
- Before a non-trivial task: `omp-memory search "<keywords>"`, and `recap --session <id>` when you have one.
- When a task ends: `omp-memory capture --task ... --outcome ... --evidence ...`. Add `--verified` only if you
  observed the result, not if you assumed it.
- When reality confirms or contradicts a record: `omp-memory reinforce <record_id> success|failure|correction`.
- `omp-memory context "<keywords>"` prints a fenced block you can drop into the prompt.
- Never capture secrets, credentials, or personal data. The CLI refuses secret-looking input; do not pass
  `--allow-secrets` unless the operator explicitly asks.
```

To install by hand into `~/.omp/memory`:

```bash
git clone https://github.com/alfar1zi/honcho-pi
cd honcho-pi
bash install.sh
source ~/.omp/memory/env.sh
omp-memory health
```

`install.sh` generates `env.sh` on the first run, so that file does not exist in the repo. Re-running the installer
replaces the code and the `bin/` shims. Your database, backups, capture logs, and audit log stay untouched.
`--dry-run` shows what would change.

## CLI

```bash
omp-memory capture --task "fix WAL corruption" --outcome "set busy_timeout" --verified
omp-memory search "wal corruption" --json
omp-memory recap --session sess-20261029T101500Z
omp-memory context "sqlite backup"
omp-memory reinforce <record_id> success        # success, failure, correction, reinforce, decay
omp-memory promote --threshold 0.85             # copies verified rows at that confidence into RULE
omp-memory checkpoint --session sess-20261029T101500Z
omp-memory restore <checkpoint_id>
```

More subcommands: `analyze`, `health`, `stats`, `backup`, `repair`, `disk`, `scan`. Pass `--json` to `search`,
`recap`, `health`, or `disk` for machine-readable output.

`capture` joins `--task` and `--outcome` into one line of the form `task -> outcome`. It scans that line and
`--evidence` for secrets before it stores anything, and `--allow-secrets` turns that scan off.

## How it works

```
capture -> sessions + records -> records_fts (FTS5, trigger-synced)
      |             |
      |             +-> reinforcement (append-only signal log)
      +-> sessions/*.jsonl (append-only)

records -> checkpoint payload (JSON) -> restore after compression
records -> export_markdown -> sync_mirror (git commit, never push)
```

Each **record** stores a `type` (FACT, ASSUMPTION, INFERENCE, DECISION, RULE, CORRECTION) plus `evidence`,
`verified`, `confidence`, `source`, `tags`, `session_id`, `project_id`, `project_ids`, `parent_id`, and
`last_decay_at`.

The library and the CLI decide "same record" in different ways, and both paths ship on purpose:

| | `remember()` (library) | `upsert_record()` (CLI) |
|---|---|---|
| Identity | the normalised text alone: NFKC, whitespace collapsed, casefolded | type, content, source, and project |
| Provenance | `source` and `project_ids` merge into `s1,s2` style lists | a second source writes a second row |
| Used by | the Python API | `omp-memory capture` |
| Auto-merge | identical text, or `upsert=True` with 0.85 similarity | exact match on all four key fields |

**Similarity** compares two texts by the words they share (Jaccard). A score of 0.50 or more only gets reported.
An automatic merge needs 0.85 plus identical numbers, because measured paraphrases land at 0.40 to 0.64 and
adversarial near-misses reach 0.82.

**Heat** is one number between 0 and 1 that ranks a record for recall. It combines `confidence` (0.45), recency
(0.25), and past success signals (0.30), and recency halves every 30 days. Without a success signal, heat tops out at
0.70.

**Promotion** (`promote_hot`) turns a hot INFERENCE or ASSUMPTION into a FACT. It needs heat of 0.80 or more and
success signals from two different sources. It never changes `verified`.

**Forgetting** (`forget_cold`) runs as a dry run unless you pass `apply=True`. A record qualifies only if it is
unverified, not a RULE or CORRECTION, at or below confidence 0.16, older than 180 days, and untouched for 365 days.
Applying writes a graveyard row before the delete.

**Decay** (`apply_decay`) pulls stale confidence down toward 0.15, halving every 30 days. It anchors on the last decay
time, so calling it twice at the same moment changes nothing.

**Search** (`records_fts`) is a SQLite FTS5 index kept in sync by triggers. Every query term is quoted before it
reaches FTS5, so a query containing `"`, `*`, `-`, or `NEAR(` is searched as text.

**Profiles** keep separate memories under one database, keyed by `(profile, key)`. **Sessions** link to their
predecessor through `parent_id`, so a recap can walk back across a compression.

## Recall

`recall()` scores at least 20 candidates (or `limit * 4` when that is larger, or `pool`) and returns the top `limit`:

```
score = 0.60 * lexical + 0.10 * confidence + 0.25 * heat + 0.05 * type_prior
```

`lexical` measures how well the text matched the query (normalised BM25 rank), and `type_prior` is a fixed bonus by
type: 1.0 for RULE and FACT, 0.85 CORRECTION, 0.60 DECISION, 0.35 ASSUMPTION, 0.25 INFERENCE. When two scores tie,
verified rows come first, then higher confidence, then newer `updated_at`, then `id`.

The default result is compact: `{id, type, confidence, verified, score, heat, snippet}`, where `snippet` is one line
of at most 120 characters cut with `...`. Pass `full=True` to swap `snippet` for `content` and `evidence`, and to add
`lexical`, `type_prior`, `age_days`, and `successes`.

```python
import omp_memory as m

conn = m.connect()
m.init_db(conn)

out = m.remember(conn, "FACT", "Install dependencies with uv, not pip", verified=1, source="s1")
print(out["id"], out["applied"])            # 7b087cacbcd14536 inserted

again = m.remember(conn, "FACT", "install dependencies with UV, not pip", source="s2")
print(again["id"] == out["id"], again["applied"])   # True updated
print(m.get_record(conn, out["id"])["source"])      # s1,s2
print(m.get_record(conn, out["id"])["confidence"])  # 0.5

for hit in m.recall(conn, "install dependencies", limit=2):
    print(hit["score"], hit["snippet"])
# 0.81875 install dependencies with UV, not pip

for hit in m.recall(conn, "install dependencies", limit=2, full=True):
    print(hit["type_prior"], hit["successes"])   # 1.0 0
```

The printed output comes from a fresh `.rlcheck/db.sqlite` created for this check. Note that `confidence` stayed at
0.5: the merge takes the confidence of the last write, and the second call passed none.

## Mirror

Two functions turn the database into Markdown and commit that directory to a git repository. Neither has a CLI
subcommand.

`export_markdown(conn, out_dir, types=None)` writes `INDEX.md` and one `<type>.md` per record type. It records a
sha256 for each file in `.honcho-mirror.json` and returns `{written, removed, unchanged, out_dir}`.

The output holds no timestamps. A second export therefore produces identical bytes and reports nothing written.
Missing `out_dir` is an error, because the function does not create directories. A symlink that points outside
`out_dir` is an error too. Each run deletes only files listed in the previous manifest, so your own notes in that
directory survive.

`sync_mirror(out_dir, message=None, init=False)` stages exactly the manifest files and commits when the staged tree is
dirty. It returns `{committed, sha, files}` or `{committed: False, reason: "clean"}`. It refuses a directory that is
not a git repository unless you pass `init=True`. It refuses when the repository root is not `out_dir`, when this
package sits inside `out_dir`, when the manifest is missing, and when a manifest path resolves outside `out_dir`.

```python
m.export_markdown(conn, "/path/to/mirror")
m.sync_mirror("/path/to/mirror", message="chore(memory): nightly export")
```

It commits locally and stops. It never pushes.

## Reliability

| Concern | Behaviour |
|---|---|
| Concurrent writers | WAL journal mode; lock wait from `OMP_MEMORY_BUSY_TIMEOUT_MS` (default 30 s) |
| Backup | `VACUUM INTO` snapshot with mode 0600, rotated to `OMP_MEMORY_MAX_BACKUPS` |
| Corrupt database | `quick_check`, then `integrity_check`; a failure raises and leaves the file in place |
| Repair | newest backup that passes `quick_check`, copied over the target file |
| Capture durability | the DB write commits first, then the JSONL line is appended |
| Secret leakage | capture scans task, outcome, and evidence before storing; `--allow-secrets` opts out |
| Disk exhaustion | capture refuses below `--min-free-mb` (default 100) |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OMP_MEMORY_DB` | `~/.omp/memory/memory.db` | database file, read at import |
| `OMP_MEMORY_BACKUP_DIR` | `~/.omp/memory/backups` | snapshot directory, read at import |
| `OMP_MEMORY_MAX_BACKUPS` | `10` | snapshots kept, read at import |
| `OMP_MEMORY_BUSY_TIMEOUT_MS` | `30000` | SQLite lock wait, read at import |
| `OMP_MEMORY_HOME` | `~/.omp/memory` | install target for `install.sh`, exported in `env.sh` |

## Agent integration

`MemoryProvider` is an abstract base class. A subclass must define `name`, `is_available`, and `initialize`:

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
        # wrap your recall results with build_memory_context_block(...)
        return ""
```

`build_memory_context_block` wraps recalled text in a `<memory-context>` fence with a system note.
`sanitize_context` and `StreamingContextScrubber` remove that fence from anything that leaves the agent, including a
fence split across streaming deltas.

## MCP server

`MemoryProvider` needs code inside the agent. For any agent that speaks Model Context Protocol, run the server
instead; it is one process per session over stdio, and it exposes the same five operations as tools.

```bash
omp-memory-mcp                                  # stdio, MCP default
python3 -m omp_memory.mcp                       # same, without the console script
HONCHO_TRANSPORT=http HONCHO_PORT=8765 \
  HONCHO_TOKENS=t1,t2 omp-memory-mcp            # self-hosted, one database per token
```

```json
{
  "mcpServers": {
    "mem": {
      "command": "omp-memory-mcp",
      "env": {"OMP_MEMORY_DB": "/home/me/.local/share/omp-memory/memory.db"}
    }
  }
}
```

| Tool | What it does |
| --- | --- |
| `recall` | Ranked search before acting: lexical match, confidence, heat, type prior. |
| `remember` | Persist a fact, decision or correction; reports `inserted` vs `updated`. |
| `reinforce` | `success`, `failure`, `correction`, `reinforce` or `decay` after observing reality. |
| `forget` | Delete a wrong record, after writing it to `forgotten_graveyard`. |
| `stats` | Counts by type, plus verified totals. |

`remember` refuses content that matches a known secret pattern (API keys, bearer tokens, private keys) rather than
storing it. `recall` accepts `type`, `project`, `verified_only` and `min_confidence`; the filtered path uses plain
`search()` ranking, everything else goes through the `recall()` reranker.

Environment: `OMP_MEMORY_DB` (exact path, wins over everything), `HONCHO_HOME` (data root), `HONCHO_MAX_RECALL`
(default 8), `HONCHO_EXTRA_TOOLS` (directories of optional `local_*`/`adapter_*` tool modules).

## Development

```bash
python -m pytest          # 176 tests, no network, no shared state
bash bin/clean-code.sh    # secrets, eval, markers, em dashes, line length
python bench/verify.py    # PASS/FAIL over the benchmark harness
```

Tests use a temporary database per test. `bench/RESULTS.md` reports dedup, retrieval, token payload, staleness, and
integrity.

## Limits

- **Heat and recall weights are policy, not fitted numbers.** This repo has no labelled dataset, so it claims no
  accuracy beyond the ordering rules above.
- **Rerank is unproven against the baseline.** Every arm of group B in `bench/RESULTS.md` scores 1.0, so that dataset
  cannot separate reranking variants. The numbers show no regression and sub-millisecond latency, nothing more.
- **pytest runs on Python 3.12 only.** On 3.10 and 3.13 the evidence is `py_compile`, an import, and harness output.
- **The mirror never pushes, and has no CLI.** `sync_mirror` makes a local commit and stops there.
- **`capture` uses the old identity rule.** Two projects that record the same lesson get two rows, so dedup does not
  cross project boundaries.
- **There is no supersession marker.** Group D of `bench/RESULTS.md` finds no such column, so an older verified record
  outranks a newer unverified one.

## License

MIT. See [LICENSE.md](LICENSE.md).
