# Content identity, heat decay, recall reranking, and markdown mirror

## Summary
- Content identity: Records are addressed by normalized content hash (`content_id`), deduplicating surface variants
  and merging source and project provenance into `project_ids` without splitting facts.
- Advisory and upsert: Added read-only write inspection via `decide()` and guarded merging in `remember()` with numeric
  token safety guards to prevent accidental overwrites.
- Heat, promotion, and forgetting: Added composite heat scoring, monotonic decay via `apply_decay()`, promotion of
  corroborated inferences via `promote_hot()`, and cold archiving via `forget_cold()`.
- Two-stage recall: Lexical candidate pooling via `search()` returning normalized scores and multi-factor reranking
  via `recall()` combining lexical match, confidence, heat, and type priors.
- Markdown mirror and benchmark harness: Deterministic export via `export_markdown()`, push-free local sync via
  `sync_mirror()`, and write-path dedup measurement across Python 3.10, 3.12, and 3.13.
- Cross-platform: `chmod` failures no longer abort a backup or a repair, git output is decoded as UTF-8, JSONL logs
  keep LF endings, and the README quick setup targets pipx on Windows, macOS, and Linux.

## Commits
| Commit | What it does |
|---|---|
| `1f45fa4` | Rewrites the memory system as an installable package with verified semantics. |
| `d1390ae` | Identifies records by content, merges identical records, and unions provenance. |
| `f223c8b` | Implements heat scoring, promotion, monotonic decay, cold archiving, and policy constants. |
| `ed7e9d3` | Measures deduplication across all write modes in benchmark group A. |
| `8daab81` | Reranks recall candidates on lexical match, confidence, heat, and type priors. |
| `e2e4f32` | Implements deterministic export_markdown with SHA-256 manifests and push-free git sync_mirror. |
| `64eb156` | Documents measured benchmark results, hardware conditions, and harness limitations in bench/RESULTS.md. |
| `90e515f` | Rewrites the README around real examples and current behaviour. |
| `cb60205` | Adds the PR description and v0.2.0 release notes. |
| `600dc5d` | Corrects the recall pool size and the example call count in the README. |
| `ef9b191` | Ignores verification databases created inside the repo. |

## What a reviewer should check
- Does `remember()` still union source and project provenance on a merge? See `tests/test_identity.py`.
- Does `remember()` keep the richest spelling on merge without flapping? See `tests/test_identity.py`.
- Does `decide()` return a three-reason contract without writing to the database? See `tests/test_similarity.py`.
- Does `promote_hot()` require two distinct sources and heat >= 0.80 without setting verified? See `tests/test_heat.py`.
- Does `apply_decay()` remain idempotent when called repeatedly for the same timestamp? See `tests/test_heat.py`.
- Does `recall()` break score ties in favor of verified records? See `tests/test_recall.py`.
- Does `sync_mirror()` report clean and skip committing when the working tree is unchanged? See `tests/test_mirror.py`.

## Measured, not claimed
- Measured module MD5 for `src/omp_memory/db.py` is `e2382e6e50d24bab0287e7255dde8d1f` across all interpreter runs.
- The test suite passes 165 tests on Python 3.12 (`python3.12 -m pytest -q`).
- Group B retrieval harness scores 1.0 across all metrics (R@5 = 1.0, hit@1 = 1.0, MRR = 1.0) on every arm, proving
  sub-millisecond latency without regressions but NOT proving that reranking outperforms baseline.

## Not in this PR
- No automatic push: `sync_mirror()` only commits to a dedicated local git repository and does not push upstream.
- No labeled dataset: benchmark tests use synthetic corpora without human relevance labels, so accuracy is unproven.
- Fixed policy weights: heat components and rerank weights use fixed policy constants rather than fitted parameters.
- Pytest not installed on 3.10 and 3.13: verification on 3.10 and 3.13 uses py_compile, imports, and harness runs.
