#!/usr/bin/env python3
"""External memory benchmark: LongMemEval oracle (ICLR 2025), stdlib only.

Corpus = 500 QA records from xiaowu0162/longmemeval-cleaned, each with an
evidence-only haystack and labeled answer sessions. We ingest only the
evidence turns (`has_answer`) into an isolated DB, then ask the *original*
question and score whether honcho recall surfaces a turn from the answer
session at any rank (R@5) and at rank 1 (hit@1).

This is an EXTERNAL probe, not comparable to published QA numbers: LongMemEval
reports LLM-judged answer accuracy; here we measure retrieval of the gold
evidence turn. No LLM, no network, no writes to the live DB.

Run: PYTHONPATH= python3.12 bench/external.py [--data /tmp/lme_oracle.json] [--limit 500]
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR.parent / "src"))
os.environ.setdefault("OMP_MEMORY_DB", str(BENCH_DIR / "db" / "external.db"))
os.environ.setdefault("OMP_MEMORY_BACKUP_DIR", str(BENCH_DIR / "db" / "backups"))
from omp_memory import db  # noqa: E402

EXPECTED_SHA256 = "821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c"


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path, verify=True):
    got = sha256(path)
    if verify and got != EXPECTED_SHA256:
        raise ValueError(f"dataset sha256 mismatch: {got} != {EXPECTED_SHA256}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_corpus(records, limit=500):
    """Yield (qa_meta, evidence_turns) for questions with labeled answers.

    `has_answer` marks the exact turns containing the answer; we store those
    as FACT records with the session id as provenance and the question date
    as evidence, so recall has both content and a time anchor to work with.
    """
    out = []
    for qa in records[:limit]:
        targets = set(qa.get("answer_session_ids") or [])
        turns = []
        for sid, session in zip(qa.get("haystack_session_ids") or [],
                                qa.get("haystack_sessions") or []):
            for t in session:
                # LongMemEval canonical retrieval gold: turns marked
                # has_answer inside an answer session. Using the whole
                # session inflates the corpus and is not the dataset's
                # own definition of evidence.
                if t.get("has_answer") and (not targets or sid in targets):
                    turns.append((sid, (t.get("content") or "").strip()))
        if turns:
            out.append((qa, turns))
    return out


def evaluate(data_path, limit=500, verify=True, db_path=None):
    qas = load(data_path, verify=verify)
    corpus = build_corpus(qas, limit=limit)
    path = db_path or os.environ["OMP_MEMORY_DB"]
    for suffix in ("", "-wal", "-shm"):
        p = Path(path + suffix)
        if p.exists():
            p.unlink()
    conn = db.connect(path)
    db.init_db(conn)
    # SHARED corpus: all questions compete against all evidence turns.
    # A per-question DB would make every query trivially hit its own 1-2
    # records (R@5=1.0 by construction), reproducing the self-referential
    # flaw this external probe is meant to fix.
    turn_ids = {}
    for qa, turns in corpus:
        for sid, text in turns:
            rid = db.upsert_record(conn, rtype="FACT", content=text[:4000],
                                   evidence="session=" + sid,
                                   source="longeval-oracle",
                                   project_id=sid)
            turn_ids.setdefault(qa["question_id"], set()).add(rid)
    conn.commit()

    r5 = hit1 = total = 0
    by_type = {}
    for qa, _turns in corpus:
        q = qa.get("question") or ""
        if not q:
            continue
        gold = turn_ids.get(qa["question_id"], set())
        if not gold:
            continue
        hits = db.recall(conn, q, limit=5)
        got = [h["id"] for h in hits]
        typ = qa.get("question_type", "?")
        slot = by_type.setdefault(typ, {"r5": 0, "hit1": 0, "n": 0})
        slot["n"] += 1
        total += 1
        if gold.intersection(got):
            r5 += 1
            slot["r5"] += 1
        if got and got[0] in gold:
            hit1 += 1
            slot["hit1"] += 1
    conn.close()
    return {
        "dataset": "xiaowu0162/longmemeval-cleaned (oracle)",
        "sha256_verified": verify,
        "questions": total,
        "records_ingested": sum(len(t) for _, t in corpus),
        "corpus_shared": True,
        "R@5": round(r5 / total, 4) if total else 0.0,
        "hit@1": round(hit1 / total, 4) if total else 0.0,
        "by_type": {k: {"R@5": round(v["r5"] / v["n"], 4),
                        "hit@1": round(v["hit1"] / v["n"], 4), "n": v["n"]}
                    for k, v in sorted(by_type.items())},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="/tmp/lme_oracle.json")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    print(json.dumps(evaluate(args.data, args.limit, not args.no_verify, args.db),
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
