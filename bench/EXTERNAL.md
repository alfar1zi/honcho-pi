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

## What is measured, and what is NOT

The oracle split is evidence-only: each question carries a haystack with
`has_answer` turns and `answer_session_ids`. We ingest every `has_answer`
turn from all questions into ONE shared DB (896 records), then ask the
original question and check whether a gold turn appears in recall top-5 /
top-1.

This measures **evidence retrieval**, NOT answer accuracy. LongMemEval's
published numbers are GPT-4o-judged QA accuracy; LoCoMo uses F1; Zep's page
uses accuracy on 1,540 questions. These are DIFFERENT metrics and are not
comparable. No "we beat Zep" claim is made or implied.

## Results (500 questions, 479 with labeled gold)

| Question type | n | R@5 | hit@1 |
|---|---:|---:|---:|
| knowledge-update | 72 | 0.9028 | 0.7361 |
| single-session-assistant | 56 | 0.8571 | 0.6607 |
| single-session-user | 64 | 0.7812 | 0.5938 |
| multi-session | 125 | 0.6880 | 0.4320 |
| temporal-reasoning | 132 | 0.6591 | 0.3712 |
| **single-session-preference** | 30 | **0.1333** | **0.0333** |
| **Overall** | **479** | **0.7098** | **0.4843** |

## Reading the result

- Internal R@5=1.0 was saturated by construction (queries derived from the
  same records). This external probe is the first that can fail.
- Strongest: knowledge-update 0.9028 and assistant-then-recall 0.8571 --
  these are the update-correctness and provenance paths the engine was
  designed for.
- Weakest: **single-session-preference 0.1333**. A user preference ("I prefer
  tea, not coffee") is a short utterance with almost no distinctive lexical
  signal; it loses to longer turns sharing the question's topic words. This
  is a real, actionable defect -- a stdlib-safe fix is a type/prior boost
  for preference-shaped records (short, `prefer/like/never` wording) or a
  PPR link between preference turns and later topic turns. Both need an A/B
  against this pinned corpus before adoption.
- temporal 0.6591 and multi-session 0.6880 are the next targets.

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
