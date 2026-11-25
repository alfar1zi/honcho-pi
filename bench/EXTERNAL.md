# External benchmark: LongMemEval (oracle split)

First **external, labeled** measurement for honcho-pi. Replaces the internal
self-referential numbers as the honest signal; the internal harness stays for
regression/latency only.

## Provenance (all SOURCE, verified 2026-11-24)

- Dataset: `xiaowu0162/longmemeval-cleaned` (ICLR 2025, MIT, 33,282 downloads)
- File: `longmemeval_oracle.json`, 15,388,478 bytes
- URL: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
- SHA-256: `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c`
  (locally computed, matches HF `x-linked-etag`)
- Adapter: `bench/external.py` (stdlib only, verifies SHA before use)
- Gold rule: canonical `process_item_flat_index` in LongMemEval's
  `src/retrieval/run_retrieval.py:209-228`. Every user turn is indexed.
  A turn is gold iff its id contains "answer" (has_answer inside an answer
  session); all other turns are decoys. Questions with no gold turn are
  skipped, exactly as the canonical evaluator does (`run_retrieval.py:400`).

## What is measured, and what is NOT

We ingest every user turn from all 500 questions (5,479 records) into one
DB, then recall with `project_id=q:<question_id>` so each question is scored
against **its own haystack** -- the same per-question scope as the canonical
evaluator (`batch_get_retrieved_context_and_eval`, `run_retrieval.py:243`).
Queries are human-authored, so this is not self-referential: the earlier
"shared corpus" variant mixed 500 different people's turns and produced a
misleading 0.1333 for preferences.

This measures **evidence retrieval** (R@5, hit@1), NOT answer accuracy.
LongMemEval's published numbers are GPT-4o-judged QA accuracy; LoCoMo uses
F1; Zep's page uses accuracy on 1,540 questions. These are DIFFERENT metrics
and are not comparable. No "we beat Zep" claim is made or implied.

## Results (500 questions, 428 with labeled gold)

| Question type | n | R@5 | hit@1 |
|---|---:|---:|---:|
| single-session-assistant | 5 | 1.0000 | 0.6000 |
| **single-session-preference** | 30 | **1.0000** | **0.5000** |
| multi-session | 125 | 0.9680 | 0.5680 |
| single-session-user | 64 | 0.9844 | 0.7500 |
| knowledge-update | 72 | 0.9306 | 0.6944 |
| temporal-reasoning | 132 | 0.9318 | 0.6288 |
| **Overall** | **428** | **0.9556** | **0.6308** |

## Reading the result

- Internal R@5=1.0 was saturated by construction (queries derived from the
  same records). This external probe uses human-authored questions and
  canonical gold labels, so it can and does fail: overall R@5 0.9556 and
  hit@1 0.6308.
- Every category retrieves its gold turn in the top 5 at >= 0.93. The
  differentiator is rank-1: single-session-user 0.7500 vs multi-session
  0.5680 -- multi-session needs synthesis across turns, so no single turn
  dominates, which is expected.
- An earlier adapter revision reported preference R@5 0.1333; that was an
  artifact of mixing 500 unrelated haystacks into one candidate pool, not
  an engine defect. Corrected by scoping recall to the question's own
  haystack, per the canonical evaluator. No engine change was made for it.
- Freshness/update correctness remains separately measured by the internal
  harness (4/4, ~8ms); this external split has a knowledge-update category
  (0.9306 R@5) that confirms the same path on human-authored questions.

## Reproduce

```bash
# dataset already staged in /tmp; re-download with the URL above if absent
PYTHONPATH= python3.12 bench/external.py --limit 500
```

The adapter refuses to run if the dataset SHA-256 does not match, so a
corrupted or substituted corpus cannot silently change the numbers.
The corpus and gold labels are immutable; no network access at runtime.

## Other datasets evaluated (not adopted)

- `Percena/locomo-mc10` raw (`raw/locomo10.json`, sha256
  `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`,
  byte-identical to `snap-research/locomo` canonical): 10 conversations,
  1,986 QA; next candidate for cross-benchmark confirmation.
- `mem0ai/dolphinbench` (2026, Apache-2.0): agent tool-use actions, not
  recall -- different task, no comparable metric.
- `xiaowu0162/LongMemEval-V2` (2026, Apache-2.0): web-agent trajectories;
  future work.

## Rejected: SANITY-style claim ledger as recallable records

Evaluated an adaptation of `verycoolapps/sanity-anti-hallucination-skill`
(13 stars, MIT, docs-only; no tests/evaluator). Its useful idea is a claim
ledger: store each agent answer with `evidence`, `confidence`, and a
verification label. We tested adding such claims as records (simulated as
`FACT` with `source=claim-ledger`, `verified=1`, `confidence=0.9`) to the
same per-question haystack, on a pinned 60-question slice of the external
benchmark (single-session-preference + knowledge-update, seed 7):

| Variant | R@5 | hit@1 | n |
|---|---:|---:|---:|
| No claim records | 0.9273 | 0.6364 | 55 |
| With claim records | 0.9273 | 0.5455 | 55 |

R@5 is unchanged but hit@1 drops ~0.09: the agent's own answer outranks
the human-authored gold turn. **Verdict: rejected.** A claim ledger must
not be a recallable memory type; if implemented at all it belongs in a
separate audit table with no FTS exposure. No engine change was made.
