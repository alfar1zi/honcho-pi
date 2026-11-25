#!/usr/bin/env python3
"""A/B: PPR restart alpha (honcho 0.15 vs HippoRAG's reported 0.5).

HippoRAG reports a damping factor of 0.5; honcho restarts the walk with
alpha=0.15. A lower restart lets mass travel further, which should help
multi-hop joins and hurt precision on loose topic graphs. Measure both on
the pinned synthetic corpus with partial-evidence probes (the only probes
that can discriminate; full-content queries saturate).

Stdlib only; throwaway DB.
Run: PYTHONPATH= python3.12 bench/ppr_ab.py [--n 2000] [--seed 7]
"""
import argparse
import json
import os
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(BENCH_DIR), "src"))
sys.path.insert(0, BENCH_DIR)
os.environ.setdefault("OMP_MEMORY_DB", os.path.join(BENCH_DIR, "db", "ppr_ab.db"))
os.environ.setdefault("OMP_MEMORY_BACKUP_DIR", os.path.join(BENCH_DIR, "db", "backups"))

import dataset  # noqa: E402
from omp_memory import db  # noqa: E402

ALPHAS = (0.15, 0.30, 0.50)


def evaluate(n, seed):
    records, queries = dataset.build(n, seed)
    path = os.environ["OMP_MEMORY_DB"]
    if os.path.exists(path):
        os.unlink(path)
    conn = db.connect(path)
    db.init_db(conn)
    for r in records:
        db.upsert_record(conn, rtype=r["rtype"], content=r["content"],
                         source=r["source"], project_id=r.get("project_id", ""),
                         verified=r.get("verified", 0))
    conn.commit()
    ids = {}
    for r in records:
        row = db.find_record(conn, r["rtype"], r["content"], r["source"],
                             r.get("project_id", ""))
        if row:
            ids[r["id"]] = row["id"]
    import random as _random
    rng = _random.Random(seed + 1)
    hard = []
    for r in records:
        if rng.random() < 0.05:
            hard.append({"text": r["subject"].replace("-", " "), "target_id": r["id"]})
            hard.append({"text": r["value"], "target_id": r["id"]})
    original = db._ppr_expand
    results = {}
    try:
        for alpha in ALPHAS:
            def patched(conn, seeds, all_rows, _a=alpha, **kw):
                return original(conn, seeds, all_rows, alpha=_a, **kw)
            db._ppr_expand = patched
            hit1 = mrr = 0.0
            total = 0
            for q in queries + hard:
                hits = db.recall(conn, q["text"], limit=5)
                target = ids.get(q["target_id"])
                total += 1
                for rank, h in enumerate(hits, 1):
                    if h["id"] == target:
                        if rank == 1:
                            hit1 += 1
                        mrr += 1.0 / rank
                        break
            results[f"alpha_{alpha:.2f}"] = {
                "hit_at_1": round(hit1 / total, 4),
                "mrr": round(mrr / total, 4), "n": total}
    finally:
        db._ppr_expand = original
    conn.close()
    best = max(results, key=lambda k: (results[k]["hit_at_1"], results[k]["mrr"]))
    return {"arms": results, "best": best,
            "incumbent": "alpha_0.15",
            "adopt": best != "alpha_0.15"
            and results[best]["hit_at_1"] > results["alpha_0.15"]["hit_at_1"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    print(json.dumps(evaluate(args.n, args.seed), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
