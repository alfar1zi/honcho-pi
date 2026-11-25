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
| **Overall** | **428** | **0.9556** | **0.6285** |

## Reading the result

- Internal R@5=1.0 was saturated by construction (queries derived from the
  same records). This external probe uses human-authored questions and
  canonical gold labels, so it can and does fail: overall R@5 0.9556 and
  hit@1 0.6285.
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

## Open: PPR restart alpha

HippoRAG reports damping 0.5; honcho restarts at alpha=0.15. `bench/ppr_ab.py`
raced 0.15 / 0.30 / 0.50 on the pinned synthetic corpus: all three returned
hit@1 0.5400, MRR 0.5472 (n=200). The arms are identical because PPR's bonus
never decides top-1 there -- lexical already dominates, so the probe cannot
falsify the constant. Verified separately that alpha does change the
normalised mass (0.58 vs 0.21 at the third node), so the null result is the
benchmark's limit, not a wiring bug. **No change made**; alpha stays 0.15
until a multi-hop-specific probe exists.

## Open: decay exponent

`DECAY_POWER_TAU_DAYS=10, DECAY_POWER_BETA=0.5` gives retention(30d)=0.50,
i.e. it is a deliberate 30-day half-life, not an approximation error.
Murre & Dros (PMC4492928, Table 5) fit a1=0.09-0.17 for savings over minutes
to 31 days, FSRS-6 learns d=0.1542; both are shallower than 0.5. But they
describe different things (nonsense-syllable savings vs fact retention), so
changing beta would be tuning to a curve honcho is not modelling. **No
change made**; flagged with the source so the decision is auditable.

## External probe 2: LoCoMo (ACL 2024)

Second external dataset, chosen because it fixes two LongMemEval blind spots:
per-turn gold labels (`dia_id`, e.g. `D1:3`) and **dated sessions**, so the
temporal path is exercised instead of inert.

- Dataset: `snap-research/locomo` `data/locomo10.json`, 2,805,274 bytes
- SHA-256: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`
  (byte-identical to the upstream GitHub file; local copy verified)
- Adapter: `bench/locomo.py`, stdlib, SHA-gated, one project per conversation
  (10 dialogues stay separate, as in the canonical setup)
- 5,882 turns ingested; 1,977 QA items scored

| Category | n | hit@1 | R@10 |
|---|---:|---:|---:|
| temporal | 320 | 0.4562 | 0.7031 |
| multi-hop | 841 | 0.4007 | 0.6742 |
| adversarial | 446 | 0.3700 | 0.6020 |
| open-domain | 89 | 0.2022 | 0.4157 |
| single-hop | 281 | 0.1957 | 0.6050 |
| **Overall** | **1,977** | **0.3647** | **--** |

### Why this is much lower than LongMemEval (0.9556 R@5)

Not a contradiction -- a different question. LongMemEval-oracle asks whether
the evidence turn is retrievable when the question was authored from that
same evidence. LoCoMo asks conversational questions ("When did Caroline go
to the LGBTQ support group?") whose answer turn says "I went to that support
group last Tuesday", with no word overlap. Lexical retrieval cannot close
that gap; the low score is an honest measurement of the lexical ceiling,
and it is the strongest argument yet for the optional semantic boundary
that the zero-dependency rule keeps parked.

This also confirms the audit's methodological finding: R@5 0.9556 was a
lexical number, not a quality number.

### Cost note

`bench/locomo.py` uses `search()` rather than `recall()`: the probe asks
whether the right turn is findable at all, and full recall (PPR + MMR +
cue/heat) is ~3x the cost across ~2k probes. Recall quality on this corpus
is a separate, later measurement.
