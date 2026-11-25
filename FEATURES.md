# Feature inventory and gaps — honcho-pi (audited 2026-11-26)

Measured against the running code, not the README. Every count below was
produced by a command; the command is named where it matters.

---

## 1. What exists today

### 1.1 MCP tools (8, from `tools/list`)

| Tool | Parameters |
|---|---|
| `recall` | query, limit, type, project, session, verified_only, min_confidence, brief, as_of |
| `remember` | content, type, evidence, verified, confidence, tags, project, source, session, importance, cue, anchor_path, valid_from, valid_until |
| `reinforce` | id, signal, source, content, evidence |
| `forget` | id |
| `stats` | — |
| `maintenance` | op (decay/sleep/forget_cold/promote/reflect/capture/review/pending/approve/reject/not_conflict), apply, staging, pending_id, a, b |
| `digest` | budget, project |
| `handoff` | max_chars |

### 1.2 CLI (22 commands)

capture, search, recap, context, reinforce, promote, analyze, health, stats,
digest, maintenance, backup, repair, mirror, skill-export, pack, unpack, disk,
scan, overview, checkpoint, restore.

### 1.3 Engine capabilities (verified in code and benchmarks)

- FTS5 bm25 with per-type divisor and column weights 0/5/1/0.5
- Synonym concept groups (22), vocabulary gate, typo repair (skipped on write)
- PPR association graph with hub penalty and weakest-link bonus
- MMR diversification (lambda 0.7)
- Bi-temporal `as_of` + `valid_from`/`valid_until`, `not_conflict` adjudication
- Decay: power law tau=10d beta=0.5 (= 30-day half-life by design), per-record `decay_tau`
- Reinforcement signals, consolidation (ratchet closed in schema v18), retrieval practice
- Spacing effect, retrieval-induced forgetting, reconsolidation gate
- Staging (`pending_writes`) with hash pinning, anti-hoarding filters
- Checksum anchors (`anchor_path`/`anchor_sha`/`stale`)
- Claim-free write taxonomy: create/update/delete/noop from string identity
- Provenance penalty for low-trust unverified sources (A/B: hit@1 0.925 → 0.9917)
- Export: markdown mirror, agentskills.io SKILL.md, zlib sync pack
- Web graph UI (server + bundled assets in the wheel)
- Session capture from omp logs, experience abstraction (PROCEDURAL/REFLECTIVE)

### 1.4 Benchmarks

| Suite | Result |
|---|---|
| LongMemEval oracle (428 q) | R@5 0.9556, hit@1 0.6285 |
| LoCoMo (1,977 q) | hit@1 0.3647, R@10 0.60–0.70 |
| Synthetic internal | R@5 saturates; regression/latency only |
| Arms race | incumbent 0.540 hit@1, no-PPR 0.530 |
| Provenance A/B | adopt, +0.067 |
| Temporal A/B | recency helps, 1.0 vs 0.7 |
| Claim-ledger A/B | reject, −0.09 |

### 1.5 Tests

480 passing, 1 skipped. Suite covers migration fidelity, concurrency,
crash safety, MCP fuzz, HTTP surface, backup round-trip, soak, handoff logic.

---

## 2. The three memory stores question (answered with measurement)

There are **four** places memory-like text lives on this machine. They are not
duplicates — measured overlap between the honcho store and the files is **1 of
744 records**. The real problem is fragmentation: four systems, none reading
the others.

| Store | Location | Format | Size | Written by |
|---|---|---|---|---|
| honcho-pi | `~/.omp/profiles/max/agent/honcho/memory.db` | SQLite + FTS5 | 744 records | MCP tools, auto-capture, `learn` |
| omp builtin | `~/.omp/profiles/max/agent/memories/<project>/` | markdown: `MEMORY.md`, `learned.md`, `memory_summary.md`, `raw_memories.md`, `rollout_summaries/`, `skills/` | 170 files, 1.0 MB | omp's own `learn` / rollout |
| Tern | `~/.local/state/tern/daemon.state` | 128 MB TDS9 stream | — | terminal state only |
| Orca | `~/.config/orca/{orchestration,agent-session-journal}.db` | SQLite | 1.2 MB | Orca app |

### 2.1 Tern is NOT a memory store

Measured in `daemon.state`: `"scrollback"` 0 occurrences, `"buffer"` 0,
`"notes"` 0. The seven `"memory"` hits are **regex-match spans from a grep
command rendered on screen** (`{"text":"memory","node":"0:0:5","language":"regex"}`).
Tern persists terminal layout, styles and rendered cells. It does not store a
chat transcript or a memory database. Nothing to integrate, nothing to
de-duplicate. `buffers/` and `canvases/` are 4 KB each and empty.

### 2.2 Double-write CONFIRMED — and it contradicts the tombstone

First reading was wrong, so it is recorded here: the top of every
`learned.md` says *"content moved to honcho… do not add notes here"*. That
looks like a retired file. It is not.

Measured on `memories/--tmp--/learned.md` (99,509 bytes, modified today):

| Measurement | Value |
|---|---|
| Entries appended **after** the tombstone | **80** |
| Of those, absent from honcho's `records` | **80 (100%)** |
| Does it contain a note from this session (`d730140`)? | **yes** |

So omp's `learn` tool keeps appending to `learned.md` while the file's own
header says memory lives in honcho. Every note written through `learn` since
the migration exists in exactly one place: a markdown file honcho never reads.
The same pattern appears across projects (ecluna 106 entries, pama-nms 46,
design-extractor 29, telnusa-nms 22).

**Answer to "jadi ke double?"**: not double-*written*, but double-*stored* —
and the two copies have already diverged completely. Nothing is duplicated;
80 recent notes are simply missing from the database that recall queries.

`reclaim:learned.md` (141), `migrate:learned.md` (100),
`reclaim:raw_memories.md` (41) = 282 records did come across, but the newest
is 2026-11-18 — before those 80 entries. The reclaim was a one-time snapshot.

### 2.3 Orca's databases

`orchestration.db` (1.0 MB) holds run/worker/task state — coordination, not
knowledge. `agent-session-journal.db` (136 KB) last modified 2026-10-29.
Orca is retired on this machine, so these are archival. No integration needed;
a one-time export would only add noise.

---

## 3. Gaps worth closing, ranked by value per effort

### Tier 1 — correctness of what already exists

| # | Gap | Evidence it matters | Effort |
|---|---|---|---|
| 1 | **Builtin `.md` drift**: 126 files changed after the reclaim, no reader | 282 imported records, newest 8 days stale | M |
| 2 | `recall` has no `offset`/pagination; 50 is the hard cap | earlier audit | S |
| 3 | `stats` has no per-source or per-project breakdown | `source` is stored but never reported | S |
| 4 | `forget` is by id only — no bulk forget by tag/project/age | operator must script loops | S |
| 5 | No `recall --explain` to show why a hit ranked where it did | heat/lexical/ppr are computed then discarded | M |

### Tier 2 — capability gaps

| # | Gap | Why | Effort |
|---|---|---|---|
| 6 | **Semantic boundary** (`Embedder` protocol, off by default) | LoCoMo hit@1 0.3647 is the measured lexical ceiling | L |
| 7 | Contradiction detection beyond numbers (negation scope, units) | currently numeric-only | M |
| 8 | `digest` has no per-project scoping in MCP (CLI has it) | parameter exists in CLI only | S |
| 9 | No memory "health score" that predicts recall failure | would catch the drift above automatically | M |
| 10 | Multi-hop join is still the weakest category (LoCoMo 0.40) | PPR helps, not enough | L |

### Tier 3 — integration (the operator's real question)

| # | Gap | Why | Effort |
|---|---|---|---|
| 11 | **Read the builtin `.md` store** as a source, incrementally | closes the fork without migrating away | M |
| 12 | Emit `MEMORY.md`/`learned.md` summaries back for omp | one store, two readers | M |
| 13 | Tern pane events → memory (a pane failing is a lesson) | `tern events` unverified | M |
| 14 | Orca journal one-time import for historical decisions | archival only | S |

---

## 4. What I would do next, in order

1. **#1 + #11 together**: a `maintenance op=ingest_md` that reads
   `memories/**/*.md` incrementally (mtime + content hash), so the 126 drifted
   files land in honcho without a second full reclaim. Acceptance: run it, and
   a query for a fact that exists only in a recent `learned.md` returns it.
2. **#3 + #5**: `stats` by source/project, and `recall --explain`. Both are
   small and both make every later decision measurable.
3. **#4**: bulk `forget` by tag/project/age. Needed to prune the 325
   auto-capture records that accumulated during the audits.
4. Only then #6 (semantic boundary), because it is the only large item and it
   should be built against a bench that shows the gap it closes.

---

## 5. Open questions that need the operator, not more analysis

1. Should omp's builtin `.md` files stay the canonical lesson store, with
   honcho as the index — or should honcho become canonical and the files
   become an export?
2. Is there a memory budget? 744 records now; auto-capture adds ~300 per heavy
   session. Is pruning desired, or is unbounded growth fine for a local store?
3. Orca's databases: export once and archive, or leave untouched?
