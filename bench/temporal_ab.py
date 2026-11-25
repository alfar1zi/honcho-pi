#!/usr/bin/env python3
"""A/B: does recency help pick the CURRENT fact from a stale one?

Honcho's external benchmark cannot test this: every record is ingested at
the same instant, so heat is constant and the recency machinery is inert.
This probe stamps records at their real times (happened_at) and asks which
of two same-topic facts recall returns first.

Arms:
  stale-first  the correct answer is the newer record (recency should win)
  no-time      identical corpus with all timestamps collapsed to one instant
               (control: recency has no signal to use)

Metric: hit@1 for the NEWER record. Stdlib only, throwaway DB.
Run: PYTHONPATH= python3.12 bench/temporal_ab.py
"""
import json
import os
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR.parent / "src"))
os.environ.setdefault("OMP_MEMORY_DB", str(BENCH_DIR / "db" / "temporal_ab.db"))
os.environ.setdefault("OMP_MEMORY_BACKUP_DIR", str(BENCH_DIR / "db" / "backups"))
import datetime  # noqa: E402

from omp_memory import db  # noqa: E402

OLD = "2025-01-05T00:00:00+00:00"
NEW = "2026-11-20T00:00:00+00:00"
NOW = "2026-11-24T00:00:00+00:00"
NOW_DT = datetime.datetime.fromisoformat(NOW)

TOPICS = [
    ("deploy timeout", "gateway timeout is thirty seconds for deploy"),
    ("cache ttl", "cache ttl is sixty seconds for sessions"),
    ("retry budget", "retry budget is three attempts for webhooks"),
    ("batch size", "batch size is two hundred rows for exports"),
    ("log retention", "log retention is fourteen days for staging"),
    ("pool size", "pool size is twenty connections for primary"),
    ("queue depth", "queue depth is fifty messages for workers"),
    ("backoff cap", "backoff cap is five minutes for retries"),
    ("upload limit", "upload limit is fifty megabytes for avatars"),
    ("cron window", "cron window is four am for compaction"),
]


def build(use_time):
    path = os.environ["OMP_MEMORY_DB"]
    for suffix in ("", "-wal", "-shm"):
        f = Path(path + suffix)
        if f.exists():
            f.unlink()
    conn = db.connect(path)
    db.init_db(conn)
    newest = {}
    for i, (topic, fact) in enumerate(TOPICS):
        pid = f"t:{i}"
        old_text = f"{fact} (previous value)"
        new_text = f"{fact} (current value)"
        db.upsert_record(conn, "FACT", old_text, source="agent", project_id=pid,
                         verified=1, confidence=0.7,
                         happened_at=OLD if use_time else NEW)
        rid = db.upsert_record(conn, "FACT", new_text, source="agent",
                               project_id=pid, verified=1, confidence=0.7,
                               happened_at=NEW)
        newest[i] = rid
    conn.commit()
    hit1 = 0
    for i, (topic, fact) in enumerate(TOPICS):
        hits = db.recall(conn, fact, limit=5, project_id=f"t:{i}", now=NOW_DT)
        if hits:
            hit1 += hits[0]["id"] == newest[i]
    conn.close()
    return {"hit@1": round(hit1 / len(TOPICS), 4), "n": len(TOPICS)}


def main():
    report = {"with_time": build(True), "collapsed_time": build(False)}
    a, b = (report["collapsed_time"]["hit@1"], report["with_time"]["hit@1"])
    report["delta"] = round(b - a, 4)
    report["verdict"] = "recency_helps" if b > a else "neutral"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
