<div align="center">

<img
  src="assets/logo.png"
  alt="honcho-pi logo"
  width="240"
/>

# honcho-pi

### Your coding agent forgets everything. This fixes that.

One SQLite file that remembers what worked, what failed, and why.
Local-first. No server, no API key, no embeddings. Nothing leaves your machine.

[Quick start](#quick-start) | [Memory universe](#the-memory-universe)
[How it works](#how-it-works) | [MCP](#connect-your-agent)

<p>
  <a href="LICENSE.md"><img alt="License: MIT" height="20"
    src="https://img.shields.io/badge/license-MIT-9b7bff.svg?style=flat-square&labelColor=0d0d10"></a>
  <img alt="Python 3.10+" height="20"
    src="https://img.shields.io/badge/python-3.10+-3a3a45.svg?style=flat-square&labelColor=0d0d10">
  <img alt="stdlib only" height="20"
    src="https://img.shields.io/badge/dependencies-stdlib_only-3a3a45.svg?style=flat-square&labelColor=0d0d10">
  <img alt="local first" height="20"
    src="https://img.shields.io/badge/data-local--first-3a3a45.svg?style=flat-square&labelColor=0d0d10">
</p>

</div>

---

<div align="center">
<img
  src="assets/screenshots/universe-hero.webp"
  alt="The memory universe: typed records orbit a core of project knowledge,
  skill families form satellite systems, threads connect related lessons"
  width="100%"
/>
</div>

<p align="center"><i>The memory universe: every lesson, decision, and skill your agent has learned, connected.</i></p>

## The problem

Every session, your agent starts from zero. It re-learns the same build command, repeats the same wrong
assumption, and rediscovers the same fix it found last week. You end up being the memory.

honcho-pi gives the agent a memory that behaves like memory: lessons that hold get stronger, lessons that
fail fade, and everything carries the evidence behind it.

## Quick start

```bash
npx honcho-pi@latest setup
```

That is the whole install. Python 3.10+ and Node 18+ are the only prerequisites. Then connect your agent:

```json
{
  "mcpServers": {
    "memory": { "command": "omp-memory-mcp" }
  }
}
```

Works with Claude Code, Codex, Cursor, and any MCP client. The agent gets six tools: `recall`, `remember`,
`reinforce`, `forget`, `stats`, `maintenance`. It uses them on its own; you never file anything.

## The memory universe

```bash
omp-memory overview
```

<div align="center">
<img
  src="assets/screenshots/universe-hover.webp"
  alt="Hovering a node lights up its threads and dims everything unrelated"
  width="100%"
/>
</div>

Everything the agent remembers, on one canvas. Each color is a record type, each thread is a relationship.
Hover a node and its connections light up. Double-click to zoom into its neighborhood. Drag nodes to
arrange your own map; the layout is saved.

**Click any node to open it:**

<div align="center">
<img
  src="assets/screenshots/detail-image.webp"
  alt="Record detail: full content, evidence, provenance, connected nodes,
  and an attached image rendered as the node thumbnail"
  width="100%"
/>
</div>

- **Read the full record**: content, evidence, confidence, where it came from.
- **Edit it**: fix a wrong lesson right there. The edit writes to the store as a logged correction,
  with evidence required. The agent sees the corrected version on its next recall.
- **Attach images**: screenshots, diagrams, terminal output. The node becomes a thumbnail.
  Agents can attach images too, through the same API.

**And a dashboard that answers one question: is the memory healthy?**

<div align="center">
<img
  src="assets/screenshots/dashboard.webp"
  alt="Dashboard: records by type, confidence buckets, capture activity per day, unlinked count, most connected nodes"
  width="100%"
/>
</div>

Cold records decay toward forgetting; hot ones survive. If the cold bucket dominates, the store needs
reinforcement, not more capture. The most-connected list shows the hubs your agent actually leans on.

The screenshots above use generated demo data (`python3 web/scripts/demo-universe.py`), not anyone's
real memory.

## How it works

```mermaid
flowchart LR
    A[Agent works] -->|lesson| B[remember]
    B --> C[(SQLite + FTS5)]
    A -->|next session| D[recall]
    C -->|ranked hits| D
    D --> A
    A -->|it worked / it failed| E[reinforce]
    E -->|confidence up / down| C
    C -->|stale, unverified| F[decay]
    F -->|cold + old| G[forget]
```

Three rules make it more than a notes file:

1. **Every record carries evidence.** A type (FACT, RULE, DECISION, CORRECTION, ...), a confidence score,
   a verified flag, and the proof behind it. A note written once never gets to masquerade as truth.
2. **Reality adjusts confidence.** When a lesson works, the agent reinforces it and it ranks higher.
   When it fails, it loses ground. Unused confidence decays with a 30-day half-life. Verified lessons
   and rules are exempt.
3. **The same lesson merges.** Content-addressed identity means "use uv, not pip" written by two sessions
   is one record with two sources, not duplicates fighting in search.

Recall is lexical (SQLite FTS5), reranked by confidence, recency, and past success. No embedding model,
no vector database, no network call. The whole store is one file you can `cp`, inspect with `sqlite3`,
and back up on your own schedule.

## CLI

The agent uses MCP; you can use the CLI:

```bash
omp-memory search "wal corruption"          # ranked search
omp-memory digest                           # always-in-context block for session start
omp-memory capture --task "fix WAL" --outcome "set busy_timeout" --verified
omp-memory pack team.pack                   # sync the store via a git-committable file
omp-memory unpack team.pack                 # merge on another machine (no server)
omp-memory skill-export <id> ./skills       # procedural memory -> installable SKILL.md
omp-memory stats                            # counts by type
omp-memory health                           # integrity + backup status
omp-memory overview                         # the universe, in your browser
```

## What the bench shows

Measured, not claimed. Two tiers, kept separate because their metrics are not comparable:

**External, labeled** (`bench/EXTERNAL.md`, LongMemEval oracle, SHA-pinned, 479 questions):
evidence retrieval R@5 **0.7098**, hit@1 **0.4843**. Strongest on knowledge-update
(0.9028 R@5) and assistant-recall (0.8571); weakest on short user preferences
(0.1333 R@5) -- an honest, actionable defect, not a number to hide.
This measures **evidence retrieval**, not QA accuracy or LoCoMo F1; no vendor
comparison is implied.

**Internal, self-referential** (`bench/RESULTS.md`, pinned synthetic corpus):
update-correctness 4/4, freshness ~8ms, R@5=1.0 saturated -- useful for
regression and latency, NOT a quality claim. Five ranking arms raced on
partial-evidence probes; incumbent wins, PPR earns +0.01 hit@1 over no-PPR.
No weight changes without a run that beats the incumbent.

Published numbers (Zep freshness 162.7s / 8-of-24 updates; mem0 v3 ADD-only)
come from vendor reports and are **not** directly comparable to our metrics.
Mem0's current code does implement update and delete with history events;
the "never invalidates" phrasing described an older published snapshot.

## Connect your agent

**MCP client** (Claude Code, Codex, Cursor, ...): the JSON block from [Quick start](#quick-start).

**Any agent with a shell**: paste this into its instructions:

```text
Set up the honcho-pi memory: run `npx honcho-pi@latest setup`, then follow its README.
```

**Python**:

```python
import omp_memory as m

conn = m.connect()
m.remember(conn, "FACT", "Install dependencies with uv, not pip", verified=1, source="s1")
hits = m.recall(conn, "install dependencies", limit=3)
```

## Reliability

| Concern | Behavior |
| --- | --- |
| Concurrent writers | WAL mode, 30 s lock wait |
| Backup | `VACUUM INTO` snapshot, rotated |
| Corrupt database | raises and keeps the file; `repair` restores from the newest usable backup |
| Secret leakage | capture, MCP writes, and the web editor scan for secrets before storing |
| Disk exhaustion | CLI capture refuses below 100 MB free |

## Configuration

| Variable | Default |
| --- | --- |
| `OMP_MEMORY_DB` | `~/.omp/memory/memory.db` |
| `OMP_MEMORY_BACKUP_DIR` | `~/.omp/memory/backups` |
| `OMP_MEMORY_MAX_BACKUPS` | `10` |
| `OMP_MEMORY_WEB_DIR` | built UI assets for `overview` |

Updates: `npx honcho-pi@latest update` (keeps the database). Status: `npx honcho-pi@latest status`.

## Development

```bash
python -m pytest              # 386 tests, no network
bash bin/clean-code.sh        # quality gate
bash bin/web-deploy.sh        # build + deploy the web UI
```

## License

MIT. See [LICENSE.md](LICENSE.md).