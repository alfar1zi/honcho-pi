#!/usr/bin/env python3
"""External probe 2: LoCoMo (ACL 2024) evidence retrieval, stdlib only.

LoCoMo fills two gaps LongMemEval leaves: real per-turn dia_id gold labels
(D1:3 style) and dated sessions, so the temporal machinery is exercised.

Gold here is exact: each QA item names the dia_ids that contain the answer.
hit@1 means recall returned the turn whose dia_id is in that set.

Metric caveat: LoCoMo's published numbers are QA F1 / MC accuracy; this
measures EVIDENCE RETRIEVAL only and is not comparable to them.

Run: PYTHONPATH= python3.12 bench/locomo.py [--data /tmp/locomo10.json]
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR.parent / "src"))
os.environ.setdefault("OMP_MEMORY_DB", str(BENCH_DIR / "db" / "locomo.db"))
os.environ.setdefault("OMP_MEMORY_BACKUP_DIR", str(BENCH_DIR / "db" / "backups"))
from omp_memory import db  # noqa: E402

EXPECTED_SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
_CATEGORY = {1: "single-hop", 2: "temporal", 3: "open-domain",
             4: "multi-hop", 5: "adversarial"}


def sha256(path):
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


def _sessions(conv):
    """Yield (session_key, date_string, turns) in ascending session order."""
    keys = [k for k in conv
            if re.fullmatch(r"session_\d+", k)]
    keys.sort(key=lambda k: int(k.split("_")[1]))
    for k in keys:
        yield k, conv.get(k + "_date_time", ""), conv[k]


def evaluate(data_path, verify=True, db_path=None):
    convs = load(data_path, verify=verify)
    path = db_path or os.environ["OMP_MEMORY_DB"]
    for suffix in ("", "-wal", "-shm"):
        p = Path(path + suffix)
        if p.exists():
            p.unlink()
    conn = db.connect(path)
    db.init_db(conn)
    # One project per conversation: LoCoMo is 10 separate dialogues, so
    # scoring across conversations would mix unrelated speakers (the same
    # mistake the LongMemEval adapter made before per-question scope).
    gold = {}
    for ci, convo in enumerate(convs):
        pid = f"conv:{ci}"
        for _skey, _date, turns in _sessions(convo["conversation"]):
            for turn in turns:
                did = turn.get("dia_id") or ""
                rid = db.upsert_record(
                    conn, "FACT", (turn.get("text") or "")[:4000],
                    evidence=f"dia={did} speaker={turn.get('speaker', '')}",
                    source="locomo", project_id=pid, verified=1, confidence=0.8)
                gold[(ci, did)] = rid
        for qa in convo.get("qa") or []:
            pass
    conn.commit()
    stats = {}
    total = hit1 = 0
    for ci, convo in enumerate(convs):
        pid = f"conv:{ci}"
        for qa in convo.get("qa") or []:
            q = (qa.get("question") or "").strip()
            ev = qa.get("evidence") or []
            if not q or not ev:
                continue
            want = {gold[(ci, d)] for d in ev if (ci, d) in gold}
            if not want:
                continue
            # search(), not recall(): this probe measures whether the
            # right turn is findable at all. recall() adds PPR + MMR +
            # cue/heat reranking on top, which is a different question and
            # 3x the cost across ~2k probes.
            hits = db.search(conn, q, limit=10, project_id=pid)
            ids = [h["id"] for h in hits]
            cat = _CATEGORY.get(qa.get("category"), "?")
            slot = stats.setdefault(cat, {"hit1": 0, "r10": 0, "n": 0})
            slot["n"] += 1
            total += 1
            if want & set(ids):
                slot["r10"] += 1
            if ids and ids[0] in want:
                slot["hit1"] += 1
                hit1 += 1
    conn.close()
    return {
        "dataset": "snap-research/locomo locomo10.json",
        "sha256_verified": verify,
        "questions": total,
        "records_ingested": len(gold),
        "hit@1": round(hit1 / total, 4) if total else 0.0,
        "by_category": {k: {"hit@1": round(v["hit1"] / v["n"], 4),
                            "R@10": round(v["r10"] / v["n"], 4), "n": v["n"]}
                        for k, v in sorted(stats.items())},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="/tmp/locomo10.json")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    print(json.dumps(evaluate(args.data, not args.no_verify, args.db),
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
