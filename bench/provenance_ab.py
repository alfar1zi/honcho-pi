#!/usr/bin/env python3
"""A/B: does a low-trust provenance penalty actually help recall?

Threat model: an auto-captured (untrusted) record paraphrases a true fact
and competes with the verified human record for top-1. The test builds the
SAME corpus in both arms; the only difference is whether recall applies a
bounded penalty to low-trust, unverified sources.

Metric: hit@1 for the VERIFIED record (not R@5 -- the attack only matters
if the poisoned copy outranks the truth).

Stdlib only; throwaway DB; live store untouched.
Run: PYTHONPATH= python3.12 bench/provenance_ab.py
"""
import json
import os
import random
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR.parent / "src"))
os.environ.setdefault("OMP_MEMORY_DB", str(BENCH_DIR / "db" / "provenance_ab.db"))
os.environ.setdefault("OMP_MEMORY_BACKUP_DIR", str(BENCH_DIR / "db" / "backups"))
from omp_memory import db  # noqa: E402

DATA = "/tmp/lme_oracle.json"
SEED = 11
POISON_RATE = 0.30


def build_cases():
    """(query, true_content) pairs from real LongMemEval evidence turns."""
    with open(DATA, encoding="utf-8") as f:
        rows = json.load(f)
    rng = random.Random(SEED)
    cases = []
    for qa in rows:
        targets = set(qa.get("answer_session_ids") or [])
        for sid, session in zip(qa.get("haystack_session_ids") or [],
                                qa.get("haystack_sessions") or []):
            if sid not in targets:
                continue
            for turn in session:
                if turn.get("role") != "user" or not turn.get("has_answer"):
                    continue
                text = (turn.get("content") or "").strip()
                if len(text) > 80:
                    cases.append((qa["question"], text))
        if len(cases) >= 120:
            break
    rng.shuffle(cases)
    return cases[:120]


def poison(text):
    """Paraphrase-shaped poison: same topic words, one wrong token."""
    return text.replace("I ", "I never ").replace("my ", "my old ")


def arm(use_penalty):
    path = os.environ["OMP_MEMORY_DB"]
    for suffix in ("", "-wal", "-shm"):
        f = Path(path + suffix)
        if f.exists():
            f.unlink()
    conn = db.connect(path)
    db.init_db(conn)
    rng = random.Random(SEED)
    truth_ids = {}
    for i, (q, text) in enumerate(build_cases()):
        pid = f"case:{i}"
        rid = db.upsert_record(conn, rtype="FACT", content=text,
                               evidence="verified human note", source="agent",
                               project_id=pid, verified=1, confidence=0.9)
        truth_ids[i] = rid
        if rng.random() < POISON_RATE:
            db.upsert_record(conn, rtype="FACT", content=poison(text),
                             evidence="scraped", source="auto-capture",
                             project_id=pid, verified=0, confidence=0.9)
    conn.commit()
    db.PROVENANCE_PENALTY_ENABLED = use_penalty
    hit1 = total = 0
    for i, (q, _text) in enumerate(build_cases()):
        hits = db.recall(conn, q, limit=5, project_id=f"case:{i}")
        if not hits:
            continue
        total += 1
        hit1 += hits[0]["id"] == truth_ids[i]
    conn.close()
    return {"hit@1": round(hit1 / total, 4), "n": total}


def main():
    report = {"no_penalty": arm(False), "with_penalty": arm(True)}
    a, b = report["no_penalty"]["hit@1"], report["with_penalty"]["hit@1"]
    report["delta"] = round(b - a, 4)
    report["verdict"] = "adopt" if b > a else ("neutral" if b == a else "reject")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
