"""Config-arm archive (mini Darwin Godel Machine, arXiv 2505.22954):
evaluate ranking weight arms on the pinned synthetic corpus, keep the
results as evidence. Each arm is a full weight config; the harness
measures hit@1/MRR per arm on the same records, so a weight change is
a measured decision, not a guess. Stdlib only; no arm is adopted
without a run that beats the incumbent on the pinned slice.

Run: PYTHONPATH= python3.12 bench/arms.py [--n 2000] [--seed 7]
"""
import argparse
import json
import os
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(BENCH_DIR), "src"))
sys.path.insert(0, BENCH_DIR)
os.environ.setdefault("OMP_MEMORY_DB", os.path.join(BENCH_DIR, "db", "arms.db"))
os.environ.setdefault("OMP_MEMORY_BACKUP_DIR", os.path.join(BENCH_DIR, "db", "backups"))

import dataset  # noqa: E402
from omp_memory import db  # noqa: E402

ARMS = {
    "incumbent": {"lexical": 0.70, "heat": 0.25, "type_prior": 0.05, "ppr": 0.10},
    "lexical_heavy": {"lexical": 0.80, "heat": 0.15, "type_prior": 0.05, "ppr": 0.10},
    "heat_heavy": {"lexical": 0.60, "heat": 0.35, "type_prior": 0.05, "ppr": 0.10},
    "no_ppr": {"lexical": 0.70, "heat": 0.25, "type_prior": 0.05, "ppr": 0.0},
    "ppr_heavy": {"lexical": 0.65, "heat": 0.25, "type_prior": 0.05, "ppr": 0.20},
}


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
    # dataset ids are r%06d; db ids are content-addressed. Map by content.
    db_id_by_dataset_id = {}
    for r in records:
        row = db.find_record(conn, r["rtype"], r["content"], r["source"], r.get("project_id", ""))
        if row:
            db_id_by_dataset_id[r["id"]] = row["id"]
    # Hard probes: subject-only and value-only queries. The full-content
    # queries saturate at 1.0 for every arm (the audit's group-B finding);
    # these force the ranker to discriminate on partial evidence.
    import random as _random
    rng = _random.Random(seed + 1)
    hard = []
    for r in records:
        if rng.random() < 0.05:
            hard.append({"text": r["subject"].replace("-", " "), "target_id": r["id"]})
            hard.append({"text": r["value"], "target_id": r["id"]})
    results = {}
    incumbent = dict(db.RECALL_WEIGHTS)
    incumbent_ppr = db.PPR_WEIGHT
    for name, w in ARMS.items():
        db.RECALL_WEIGHTS = {k: w[k] for k in ("lexical", "heat", "type_prior")}
        db.PPR_WEIGHT = w["ppr"]
        hit1, mrr, total = 0, 0.0, 0
        for q in queries + hard:
            hits = db.recall(conn, q["text"], limit=5)
            target = db_id_by_dataset_id.get(q["target_id"])
            total += 1
            for rank, h in enumerate(hits, 1):
                if h["id"] == target:
                    if rank == 1:
                        hit1 += 1
                    mrr += 1.0 / rank
                    break
        results[name] = {"hit_at_1": round(hit1 / total, 4),
                         "mrr": round(mrr / total, 4), "n": total}
    db.RECALL_WEIGHTS = incumbent
    db.PPR_WEIGHT = incumbent_ppr
    conn.close()
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    results = evaluate(args.n, args.seed)
    best = max(results, key=lambda k: (results[k]["hit_at_1"], results[k]["mrr"]))
    report = {"corpus": {"n": args.n, "seed": args.seed},
              "arms": results, "best": best,
              "incumbent": "incumbent",
              "adopt": best != "incumbent" and
                       results[best]["hit_at_1"] > results["incumbent"]["hit_at_1"]}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
