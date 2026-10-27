# Release Notes - v0.2.0

## Added
- `content_id(content: str) -> str`: Compute deterministic identifier from normalized content text.
- `normalize_content(content: str) -> str`: Normalize Unicode (NFKC), strip outer spaces, and collapse whitespace runs.
- `identity_text(content: str) -> str`: Return normalized, casefolded text used for content addressing.
- `find_similar(conn, content, limit=5, threshold=SIMILAR_THRESHOLD, rtype=None) -> list`: Find lexical candidates
  scored by Jaccard similarity above threshold.
- `decide(conn, content, rtype="FACT", limit=5, threshold=SIMILAR_THRESHOLD, upsert=False) -> dict`: Read-only helper
  reporting action, reason (identical, similar, novel), candidates, and advisories.
- `remember(conn, rtype, content, evidence="", verified=0, confidence=0.5, source="", tags="", session_id="",`
  `project_id="", parent_id="", upsert=False, limit=5) -> dict`: Write memory by content identity, merging source
  and project provenance.
- `recall(conn, query, limit=8, full=False, pool=None, rtype=None, project_id=None, now=None) -> list`: Retrieve and
  rerank candidates using lexical relevance, confidence, heat, and type prior.
- `promote_hot(conn, record_id=None, now=None)`: Promote corroborated inferences or assumptions with heat >=
  `PROMOTE_HEAT` across two distinct sources to facts.
- `last_promotion()`: Return promotion metadata recorded by the most recent reinforcement on the current thread.
- `forget_cold(conn, apply=False, now=None, floor=DECAY_FLOOR, min_age_days=FORGET_MIN_AGE_DAYS,`
  `window_days=FORGET_WINDOW_DAYS) -> dict`: Identify and optionally archive cold records into `forgotten_graveyard`.
- `export_markdown(conn, out_dir, types=None) -> dict`: Export partitioned markdown documents and write a SHA-256
  manifest (`.honcho-mirror.json`).
- `sync_mirror(out_dir, message=None, init=False) -> dict`: Stage manifest files and commit changes to a dedicated
  local git repository without pushing.

## Changed
- `remember()` now unions `source` and `project_ids` provenance instead of keeping previous values.
- Content identity no longer separates records by source or project; identical normalized content shares one row.
- `search()` now returns an additional `lexical` score field (normalized between 0.0 and 1.0) and sorts records by
  `verified` before `confidence`.

## Schema
- SCHEMA_VERSION upgraded from 2 to 5.
- Migration v3 adds `project_ids TEXT NOT NULL DEFAULT ''` to `records` table so merged records accumulate project
  provenance.
- Migration v4 introduces audit tables `forgotten_graveyard` (recording archived cold rows) and `promotion_log`
  (recording inference-to-fact promotions).
- Migration v5 adds `last_decay_at TEXT NOT NULL DEFAULT ''` to `records` table to anchor decay calculations and ensure
  monotonic decay across repeated calls for the same timestamp.
- All schema migrations are idempotent and never delete or overwrite existing rows.

## Fixed
- Fixed source provenance bug where a second observation of an existing fact dropped the new source value on merge.
- Restored `StreamingContextScrubber` export in `omp_memory.__all__`.

## Not yet
- No automatic push: `sync_mirror()` only commits to a dedicated local git repository and does not push upstream.
- No labeled dataset: benchmark tests use synthetic corpora without human relevance labels, so accuracy is unproven.
- Fixed policy weights: heat components and rerank weights use fixed policy constants rather than fitted parameters.
- Pytest not installed on 3.10 and 3.13: verification on 3.10 and 3.13 uses py_compile, imports, and harness runs.
