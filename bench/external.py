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
import re
import sqlite3
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR.parent / "src"))
os.environ.setdefault("OMP_MEMORY_DB", str(BENCH_DIR / "db" / "external.db"))
os.environ.setdefault("OMP_MEMORY_BACKUP_DIR", str(BENCH_DIR / "db" / "backups"))
from omp_memory import db  # noqa: E402

EXPECTED_SHA256 = "821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c"


_DATE_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})")


def parse_date(value: str) -> str:
    """LongMemEval dates look like "2023/04/10 (Mon) 17:50", not ISO.

    Returning "" when unparseable is deliberate: upsert_record treats an empty
    happened_at as "now", so a malformed date degrades to the old behaviour
    instead of raising mid-ingest.
    """
    match = _DATE_RE.match((value or "").strip())
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{month}-{day}T00:00:00+00:00"


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
        dates = dict(zip(qa.get("haystack_session_ids") or [],
                         qa.get("haystack_dates") or []))
        for sid, session in zip(qa.get("haystack_session_ids") or [],
                                qa.get("haystack_sessions") or []):
            for t in session:
                # Canonical LongMemEval (`process_item_flat_index`,
                # src/retrieval/run_retrieval.py:217-228): every USER turn
                # is indexed, including non-answer decoys. Gold turns are
                # those whose doc_id contains "answer" -- i.e. has_answer
                # inside an answer session. We carry the flag in evidence
                # so gold selection below matches the canonical rule.
                if t.get("role") != "user":
                    continue
                txt = (t.get("content") or "").strip()
                if txt:
                    turns.append((sid, txt, bool(t.get("has_answer")),
                                  parse_date(dates.get(sid, ""))))
        if turns:
            out.append((qa, turns))
    return out


def evaluate(data_path, limit=500, verify=True, db_path=None, expand=0):
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
        qid = qa["question_id"]
        for sid, text, is_gold, when in turns:
            # project_id isolates per-question gold sets: the same session
            # text can occur in two questions with different gold labels.
            # A content-addressed id with project_id=sid would merge them
            # and silently drop 72 questions (500 -> 428).
            rid = db.upsert_record(conn, rtype="FACT", content=text[:4000],
                                   evidence=f"session={sid} gold={int(is_gold)}",
                                   source="longeval-oracle",
                                   project_id=f"q:{qid}",
                                   happened_at=when)
            turn_ids.setdefault(qid, set()).add(rid)
    conn.commit()

    r5 = hit1 = total = 0
    by_type = {}
    for qa, _turns in corpus:
        q = qa.get("question") or ""
        if not q:
            continue
        # Gold = every indexed turn whose session is an answer session.
        # Stored project_id is the session id, so we select by provenance.
        gold = set()
        for rid in turn_ids.get(qa["question_id"], set()):
            row = conn.execute("SELECT project_id, evidence FROM records WHERE id=?",
                               (rid,)).fetchone()
            if row and "answer" in (row[1] or "") and "gold=1" in (row[1] or ""):
                gold.add(rid)
        if not gold:
            continue
        # Canonical LongMemEval evaluates each question against ITS OWN
        # haystack (run_retrieval.py:243). Scoping by project_id reproduces
        # that: the question's decoys stay in the candidate pool, but turns
        # from 499 other people cannot crowd the top-5. Queries are human
        # authored, so this is not self-referential.
        hits = db.recall(conn, q, limit=5, project_id=f"q:{qa['question_id']}",
                         expand=expand)
        got = [h["id"] for h in hits]
        typ = qa.get("question_type", "?")
        slot = by_type.setdefault(typ, {"r5": 0, "hit1": 0, "n": 0})
        slot["n"] += 1
        total += 1
        window_ids = set()
        for h in hits:
            window_ids |= {w["id"] for w in (h.get("window") or [])}
        covered = gold.intersection(set(got) | window_ids)
        if covered:
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
        "scope": "per-question haystack (canonical LongMemEval)",
        "expand": expand,
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
    ap.add_argument("--expand", type=int, default=0,
                    help="window size; gold found in a hit's window counts")
    args = ap.parse_args()
    print(json.dumps(evaluate(args.data, args.limit, not args.no_verify, args.db,
                              expand=args.expand),
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
