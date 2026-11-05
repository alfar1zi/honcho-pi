<div align="center">

<img src="assets/logo.png" alt="honcho-pi logo" width="280" />

# honcho-pi

### Evidence-backed local memory for coding agents

One SQLite file that stores what worked, so the next session starts where the last one stopped.

[Quick setup](#quick-setup) | [MCP server](#mcp-server) | [CLI](#cli) | [How it works](#how-it-works)

</div>

---

**honcho-pi is not a black box.** Every record carries a type (FACT, DECISION, CORRECTION, ...), a confidence
score, a `verified` flag, and the evidence behind it. A lesson that failed loses ground instead of hardening. A
note that was never re-checked does not get to masquerade as truth. The whole store is one file you can copy
with `cp`, inspect with `sqlite3`, and commit to your own backup schedule.

There is no server, no API key, and no embedding model. Nothing leaves the machine.

```bash
npx skills add alfar1zi/honcho-pi
```

That command installs honcho-pi as an agent skill. To install the memory itself, see [Quick setup](#quick-setup).

## Why it exists

An agent forgets every lesson at session end. It re-learns the same build command and repeats the same wrong
assumption next week. Three failures follow from that:

1. **Memory that lies.** A note written once and never re-checked becomes confidently wrong. Each record tracks
   `verified`, `confidence`, and an append-only signal log, so a lesson that fails loses ground instead of hardening.
2. **Memory that vanishes.** Compression and context resets erase recent lessons. Capture commits the DB row first,
   then appends a line to a JSONL file. Checkpoint and restore move a session payload through a compression.
3. **Memory that corrupts.** A raw copy of a live database can capture a half-written state. Some repair tools delete
   the file when it fails to open. honcho-pi snapshots with `VACUUM INTO` and never deletes a database it cannot open.

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

## Quick setup

One command, no clone, no `cd`, works the same on Linux, macOS, and Windows. The installer needs Python 3.10+ and
Node 18+ on PATH; everything else it downloads. The database defaults to `~/.omp/memory/memory.db`; set
`OMP_MEMORY_DB` to move it.

```bash
npx honcho-pi@latest setup
```

`npx honcho-pi@latest setup` clones the repo to a temp directory, pins it to the newest commit, creates a virtualenv
in the install directory, pip installs the package, and runs `omp-memory health` as a smoke test. The system
Python is never touched. On Windows the install lands under `%APPDATA%\omp-memory` and the CLI under
`venv\Scripts\omp-memory.exe`; on Linux and macOS it is `~/.omp/memory/venv/bin/omp-memory`.

Upgrade later without cloning anything:

```bash
npx honcho-pi@latest update     # reinstalls at the newest commit; keeps the database
npx honcho-pi@latest status     # compares the installed commit with the newest
```

### Install by hand

`pipx` installs the CLI into its own environment, so it works the same on Linux, macOS, and Windows and never trips
the "externally managed environment" block that plain `pip` hits on Debian and Ubuntu:

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
`\Scripts\python.exe` instead of `bin/python`.

### Connect an agent

**Any MCP client.** The MCP server exposes the memory as five tools over stdio. Add this to the agent's MCP
configuration:

```json
{
  "mcpServers": {
    "memory": { "command": "omp-memory-mcp" }
  }
}
```

**Any agent that can run shell commands.** Paste this one-liner into the agent's instructions; it installs and
uses the memory on its own:

```text
Set up the honcho-pi memory: run `npx honcho-pi@latest setup`, then follow its README.
```

**Python code.** `MemoryProvider` is an abstract base class. A subclass must define `name`, `is_available`, and
`initialize`; see [Agent integration](#agent-integration).

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
| `OMP_MEMORY_HOME` | `~/.omp/memory` | install target of the npx setup command |

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

## Self-update

The install directory keeps a marker file, `install.json`, with the commit it was installed from. `npx
honcho-pi@latest update` compares that commit with the newest on the repository, and when they differ it rebuilds
the virtualenv at the newest commit. The database, backups, capture logs, and audit log live in the same install
directory and survive every update. `npx honcho-pi@latest status` shows both commits and prints the update command
when one exists.

There is no background daemon and no scheduled job; updates happen when the command is run. An agent that opens a
session can run `npx honcho-pi@latest status` and apply the update itself when the output says one is available.

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
- **No coverage number is reported.** The suite asserts behaviour; it does not measure line coverage, and no
  accuracy claim rests on it.
- **The mirror never pushes, and has no CLI.** `sync_mirror` makes a local commit and stops there.
- **`capture` uses the old identity rule.** Two projects that record the same lesson get two rows, so dedup does not
  cross project boundaries.
- **There is no supersession marker.** Group D of `bench/RESULTS.md` finds no such column, so an older verified record
  outranks a newer unverified one.

## License

MIT. See [LICENSE.md](LICENSE.md).
