---
name: memory
description: Persistent memory for coding agents backed by a local SQLite file. Use when the user says "remember this", "save that lesson", "what did we decide last time", "why did we pick X", "recap this session", "we solved this before", or asks to keep context across sessions, compactions, or a new chat.
license: MIT
---

# Memory

One SQLite file on disk holds what the agent learned. Capture a lesson when a
task ends, search it when a similar task starts. Nothing leaves the machine.

## When to use

Trigger this skill when the user:

- says "remember", "save this", "note that", "don't forget"
- asks what was decided before: "why did we pick X", "what did we try last time"
- asks for a recap of a session or a project
- starts a task that a previous session already solved
- is about to compact or reset context and wants the important parts kept

Do not capture secrets, credentials, or personal data. The capture command
refuses secret-looking input; do not override it with `--allow-secrets` unless
the user explicitly asks.

## Capture

```bash
omp-memory capture \
  --task "what was attempted" \
  --outcome "what actually happened" \
  --type FACT \
  --evidence "how you know: command output, test name, file:line" \
  --verified
```

- `--type`: `FACT`, `ASSUMPTION`, `INFERENCE`, `DECISION`, `RULE`, `CORRECTION`.
- `--verified` only when the outcome was observed, not assumed.
- `--confidence` 0.0 to 1.0; default 0.5. Use 0.9+ only with evidence.
- `--project` defaults to a hash of the git remote, so records stay scoped.

Capture the *lesson*, not the transcript. `"ran migrations twice" -> "duplicate
index error; drop before create"` is useful. A pasted log is not.

## Recall

```bash
omp-memory search "migration duplicate index"
omp-memory search "sqlite" --type DECISION --verified --json
omp-memory context "migration"     # fenced block for the prompt
```

Read the `confidence` and `verified` fields before trusting a result. An
unverified `INFERENCE` at 0.5 is a hypothesis, not a fact.

## Recap

```bash
omp-memory recap --session sess-20261029T101500Z
omp-memory recap --project <hash>
```

Use a recap before summarizing work, and before writing a handoff.

## Reinforce

```bash
omp-memory reinforce <record_id> success
omp-memory reinforce <record_id> failure --source "test run"
```

Signals: `success`, `failure`, `correction`, `reinforce`, `decay`. Confidence is
clamped to [0, 1] and every signal is logged. A lesson that keeps failing should
not survive as a rule.

## Promote

```bash
omp-memory promote --threshold 0.85
```

Verified records above the threshold become `RULE` records with a `parent_id`
pointing at the source. Promote deliberately, not on a schedule.

## Before compaction

```bash
omp-memory checkpoint --session "$SESSION_ID"
omp-memory restore <checkpoint_id>
```

The checkpoint stores the full record payload as JSON, so restore works even if
the live table was cleared.

## Operations

```bash
omp-memory health            # status, db path, record and backup counts
omp-memory backup            # consistent snapshot, never a raw copy
omp-memory repair            # restore from the newest backup that passes quick_check
omp-memory stats             # counts by type
omp-memory disk 100          # refuse work below 100MB free
```

Run `health` first whenever memory looks wrong. Run `backup` before anything
destructive. Run `repair` only when `health` reports `corrupt`, and never delete
the database by hand: the tool refuses to destroy data, so should you.

## Failure modes to avoid

- Capturing a summary of a summary. Store the original lesson and evidence.
- Marking `--verified` because a command exited 0. Verify the outcome, not the
  exit code.
- Capturing one record per file edited. One record per lesson.
- Searching with a full sentence. Use the two or three distinctive words.
- Trusting a `RULE` that was never reinforced. Check its reinforcement history.
