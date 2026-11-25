#!/usr/bin/env python3
"""A/B: does a SANITY-style claim ledger help or hurt retrieval?

Pinned 60-question slice (single-session-preference + knowledge-update,
seed 7) from the SHA-pinned LongMemEval oracle. Claims are simulated as
FACT records with source=claim-ledger, verified=1, confidence=0.9 -- the
strongest possible framing, so a negative result is conclusive.

Stdlib only; isolated DB; no writes to the live store.
Run: PYTHONPATH= python3.12 bench/claims_ab.py
"""
import json
import os
import random
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR.parent / "src"))
os.environ.setdefault("OMP_MEMORY_DB", str(BENCH_DIR / "db" / "claims_ab.db"))
os.environ.setdefault("OMP_MEMORY_BACKUP_DIR", str(BENCH_DIR / "db" / "backups"))
from omp_memory import db  # noqa: E402

DATA = "/tmp/lme_oracle.json"
TYPES = ("single-session-preference", "knowledge-update")
SEED = 7
N = 60


def questions():
    with open(DATA, encoding="utf-8") as f:
        rows = json.load(f)
    pool = [q for q in rows if q.get("question_type") in TYPES]
    return random.Random(SEED).sample(pool, N)


def build(path, with_claims):
    for suffix in ("", "-wal", "-shm"):
        p = Path(path + suffix)
        if p.exists():
            p.unlink()
    conn = db.connect(path)
    db.init_db(conn)
    gold = {}
    for q in questions():
        qid = q["question_id"]
        for sid, session in zip(q.get("haystack_session_ids") or [],
                                q.get("haystack_sessions") or []):
            for turn in session:
                if turn.get("role") != "user":
                    continue
                text = (turn.get("content") or "").strip()[:4000]
                if not text:
                    continue
                rid = db.upsert_record(
                    conn, rtype="FACT", content=text,
                    evidence=f"session={sid} gold={int(bool(turn.get('has_answer')))}",
                    source="longeval-oracle", project_id=f"q:{qid}")
                gold.setdefault(qid, set()).add(rid)
        if with_claims:
            answer = str(q.get("answer") or "")[:4000]
            if answer:
                db.upsert_record(conn, rtype="FACT", content=answer,
                                 evidence=f"CLAIM of {qid}",
                                 source="claim-ledger", project_id=f"q:{qid}",
                                 verified=1, confidence=0.9)
    conn.commit()
    return conn, gold


def measure(conn, gold):
    r5 = hit1 = total = 0
    for q in questions():
        qid = q["question_id"]
        gold_ids = set()
        for rid in gold.get(qid, set()):
            row = conn.execute("SELECT evidence FROM records WHERE id=?",
                               (rid,)).fetchone()
            if row and "answer" in (row[0] or "") and "gold=1" in (row[0] or ""):
                gold_ids.add(rid)
        if not gold_ids:
            continue
        hits = db.recall(conn, q["question"], limit=5, project_id=f"q:{qid}")
        ids = [h["id"] for h in hits]
        total += 1
        r5 += bool(gold_ids.intersection(ids))
        hit1 += bool(ids and ids[0] in gold_ids)
    return {"R@5": round(r5 / total, 4), "hit@1": round(hit1 / total, 4),
            "n": total}


def main():
    path = os.environ["OMP_MEMORY_DB"]
    report = {}
    for label, with_claims in (("no_claims", False), ("with_claims", True)):
        conn, gold = build(path, with_claims)
        report[label] = measure(conn, gold)
        conn.close()
    report["verdict"] = ("reject" if report["with_claims"]["hit@1"] <
                         report["no_claims"]["hit@1"] else "no_harm")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
