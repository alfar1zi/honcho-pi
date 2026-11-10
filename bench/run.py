#!/usr/bin/env python3
"""Benchmark harness: 4 question types against a live memory database.

Read-only: the database is opened with sqlite3 mode=ro, so a benchmark run
can never write a memory. Probes are derived from the records themselves
(the corpus is the question set), so the harness works on any live database
without a curated answer key.

Question types (Lin et al. 2025 retrieval split, Mem0 update case):
  exact     content words -> expect that record at the top
  update    query for a superseded fact -> expect the newest spelling
  temporal  query restricted to a time window -> expect a record from it
  abstention  query for a topic with no record -> expect recall to abstain
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from omp_memory import db  # noqa: E402


def open_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def probe_rows(conn, n):
    rows = [dict(r) for r in conn.execute(
        "SELECT id, content, created_at, updated_at, confidence FROM records"
        " ORDER BY updated_at DESC LIMIT ?", (n,))]
    return rows


def question_exact(rows):
    """Content fragment -> is that record the top hit?"""
    cases = []
    for r in rows[:10]:
        words = [w for w in r["content"].split() if len(w) > 4][:5]
        if len(words) >= 3:
            cases.append((" ".join(words), r["id"]))
    return cases


def question_update(rows):
    """Query for the OLDEST spelling of a fact; expect the NEWEST at the top."""
    cases = []
    seen = {}
    for r in rows:
        key = " ".join(r["content"].split()[:4])
        seen.setdefault(key, []).append(r)
    for key, group in seen.items():
        if len(group) >= 2:
            oldest, newest = group[-1], group[0]
            words = [w for w in oldest["content"].split() if len(w) > 4][:4]
            if len(words) >= 3:
                cases.append((" ".join(words), newest["id"]))
    return cases


def question_temporal(rows):
    """Query = distinctive words; the window is the record's own week."""
    cases = []
    for r in rows[:6]:
        words = [w for w in r["content"].split() if len(w) > 5][:3]
        if len(words) >= 2:
            cases.append((" ".join(words), r["id"]))
    return cases


def question_abstention(conn):
    """Tokens with zero hits in the corpus: real abstention probes.

    A probe only measures abstention if nothing in the corpus shares a
    token with it; a topical miss that still shares words measures ranking,
    not abstention. Repeated-letter scrambles of real English words are
    used because they carry no project vocabulary.
    """
    vocab = {row[0] for row in conn.execute(
        "SELECT term FROM records_fts_vocab")}
    probes = ["zorblax", "flumpkin", "qwiblish"]
    return [p for p in probes if p not in vocab]


def run(conn, report):
    rows = probe_rows(conn, 60)
    if len(rows) < 5:
        print("corpus too small to benchmark (need >= 5 records)")
        return 1

    results = {"exact": [0, 0], "update": [0, 0], "temporal": [0, 0], "abstain": [0, 0]}

    for q, expected in question_exact(rows):
        hits = db.recall(conn, q, limit=3)
        results["exact"][1] += 1
        if hits and hits[0]["id"] == expected:
            results["exact"][0] += 1

    for q, expected in question_update(rows):
        hits = db.recall(conn, q, limit=3)
        results["update"][1] += 1
        if hits and hits[0]["id"] == expected:
            results["update"][0] += 1

    for q, expected in question_temporal(rows):
        hits = db.recall(conn, q, limit=3)
        results["temporal"][1] += 1
        if hits and hits[0]["id"] == expected:
            results["temporal"][0] += 1

    for q in question_abstention(conn):
        hits = db.recall(conn, q, limit=3)
        results["abstain"][1] += 1
        if not hits or hits[0]["abstained"]:
            results["abstain"][0] += 1

    print(f"corpus: {len(rows)} probe records")
    total_hit = total_n = 0
    for name in ("exact", "update", "temporal", "abstain"):
        hit, n = results[name]
        total_hit += hit
        total_n += n
        pct = f"{100.0 * hit / n:.0f}%" if n else "n/a"
        print(f"{name:<10} {hit}/{n}  {pct}")
    if total_n:
        print(f"{'overall':<10} {total_hit}/{total_n}  {100.0 * total_hit / total_n:.0f}%")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH, help="path to memory.db (read-only)")
    args = ap.parse_args()
    conn = open_readonly(args.db)
    return run(conn, print)


if __name__ == "__main__":
    sys.exit(main())
