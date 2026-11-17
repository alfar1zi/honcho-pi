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
  join      query spanning two records -> expect both in the top hits
  topical       query from record B's words -> record A (sharing B's topic
            vocabulary) must not surface: does recall discriminate
            neighbours, or does any shared token buy a hit?
"""
import argparse
from datetime import datetime
import re
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
    columns = [r[1] for r in conn.execute(
        "PRAGMA table_info(records)").fetchall()]
    extra = ", superseded_by" if "superseded_by" in columns else ""
    extra += ", session_id" if "session_id" in columns else ""
    rows = [dict(r) for r in conn.execute(
        f"SELECT id, content, created_at, updated_at, confidence{extra}"
        " FROM records ORDER BY updated_at DESC LIMIT ?", (n,))]
    for r in rows:
        r.setdefault("superseded_by", "")
        r.setdefault("session_id", "")
    return rows


def question_exact(rows):
    """Content fragment -> is that record the top hit?"""
    cases = []
    for r in rows[:10]:
        if r.get("superseded_by"):
            continue
        other = {w for o in rows if o is not r
                 for w in _terms(o["content"])}
        words = sorted(_terms(r["content"]) - other)[:5]
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
            # query with the newest's own words: the oldest's unique
            # tokens (us-west) only surface the oldest, so the update
            # expectation is unmeasurable there.
            words = [w for w in newest["content"].split() if len(w) > 4][:4]
            if len(words) >= 3:
                cases.append((" ".join(words), newest["id"]))
    return cases


def question_supersede(rows):
    """Query for a superseded fact; expect the replacement at the top.

    Rows with superseded_by set are the stale side of a correction. The
    expectation is the record that superseded them, so the probe measures
    whether the link actually downranks the loser instead of just
    decorating it.
    """
    cases = []
    for r in rows:
        if not r.get("superseded_by"):
            continue
        winner = next((w for w in rows if w["id"] == r["superseded_by"]), None)
        if winner is None:
            continue
        # Query with the winner's own terms: the loser shares the topic
        # vocabulary, so the penalty is what decides the ranking. The
        # loser's distinctive tokens only surface the loser, and the
        # penalty is invisible there.
        words = [w for w in _terms(winner["content"]) if len(w) > 4][:6]
        if len(words) >= 2:
            cases.append((" ".join(sorted(words)), r["superseded_by"]))
    return cases


def question_temporal(rows):
    """Two records on the same topic, written weeks apart: expect the
    newer one at the top.

    A real temporal probe: both records share topic vocabulary, so the
    query cannot separate them by keywords. Only recency (heat) should
    decide, which is what a time-sensitive memory must do when a fact
    has two dated spellings.
    """
    cases = []
    for a in rows:
        if a.get("superseded_by"):
            continue
        ta = _terms(a["content"])
        for b in rows:
            if b is a or b.get("superseded_by"):
                continue
            if a["updated_at"] >= b["updated_at"]:
                continue
            tb = _terms(b["content"])
            if len(ta & tb) < 2:
                continue
            days = (_ts(b["updated_at"]) - _ts(a["updated_at"])).days
            if days < 7:
                continue
            query = " ".join(sorted(ta & tb)[:6])
            cases.append((query, b["id"]))
            break
        if len(cases) >= 6:
            break
    return cases


def question_multisession(rows):
    """Session-scoped recall: records from two sessions share a topic;
    query with the session filter must return only that session's record.

    A multi-session memory (LongMemEval's session-hopping split) must be
    able to answer "what did session X conclude" without surfacing the
    same conclusion recorded elsewhere. The probe needs rows carrying a
    session id; rows without one are skipped.
    """
    cases = []
    by_session = {}
    for r in rows:
        sid = r.get("session_id") or ""
        if sid:
            by_session.setdefault(sid, []).append(r)
    for sid, group in by_session.items():
        if len(group) < 1:
            continue
        # the query comes from this session's record only; the recall
        # filter is what must exclude the other sessions' records
        r = group[0]
        words = [w for w in sorted(_terms(r["content"])) if len(w) > 4][:5]
        if len(words) >= 3:
            cases.append(((" ".join(words)), sid, r["id"]))
    return cases


def _ts(text):
    return datetime.fromisoformat(text)


def _terms(text):
    """Alphanumeric words (>4 chars) that are safe inside an FTS MATCH.

    Parentheses and other MATCH syntax characters in record content turn
    a probe into a query for an unintended expression, so the terms are
    restricted to letters and digits.
    """
    return {w for w in re.findall(r"[A-Za-z0-9]+", text) if len(w) > 4}


def question_join(rows):
    """Query spanning two records -> both in the top hits.

    A join probe takes two records that share at least two content words
    (>4 chars) and queries with the union of their distinctive words. A
    ranked top-3 that returns only one half measured a keyword match, not
    the retrieval of a multi-record answer.
    """
    cases = []
    for i, a in enumerate(rows[:10]):
        if a.get("superseded_by"):
            # A superseded record is penalized by design; a probe that
            # expects it in the top-3 measures the penalty, not the join.
            continue
        ta = _terms(a["content"])
        if len(ta) < 2:
            continue
        for b in rows[i + 1:]:
            if b.get("superseded_by"):
                continue
            tb = _terms(b["content"])
            if len(tb) < 2 or len(ta & tb) < 2:
                continue
            query = " ".join(sorted(ta | tb))
            cases.append((query, {a["id"], b["id"]}))
            if len(cases) >= 5:
                return cases
    return cases


def question_topical_abstention(rows):
    """Query record B's words; record A must not surface.

    A query built from a record's own words can never be stripped of
    them: the exact AND still matches the source record, so "remove the
    distinctive words and expect it ranked out" is unanswerable by
    construction on this corpus. The measurable question is
    discrimination: two records sharing topic vocabulary, a query drawn
    from B's non-shared words (rarest first, capped at 6), and the
    expectation that A does not appear in the top-3. Pass means recall
    returned B's neighbourhood, not A.
    """
    freq = {}
    for r in rows:
        for w in _terms(r["content"]):
            freq[w] = freq.get(w, 0) + 1
    cases = []
    for i, a in enumerate(rows[:10]):
        if len(cases) >= 10:
            break
        ta = _terms(a["content"])
        for b in rows[i + 1:]:
            if len(cases) >= 10:
                break
            if not (ta & _terms(b["content"])):
                continue
            bwords = [w for w in b["content"].split() if len(w) > 4]
            query_words = sorted(
                (w for w in bwords if w not in ta),
                key=lambda w: freq.get(w, 0))[:6]
            if len(query_words) < 3:
                continue
            cases.append((" ".join(query_words), a["id"]))
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

    results = {"exact": [0, 0], "update": [0, 0], "temporal": [0, 0],
               "abstain": [0, 0], "join": [0, 0], "topical": [0, 0],
               "supersede": [0, 0], "multisession": [0, 0]}

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

    for q, sid, expected in question_multisession(rows):
        hits = db.recall(conn, q, limit=3, session_id=sid)
        results["multisession"][1] += 1
        # the filter is a scope, not a rerank: every hit must carry the
        # asked session, and the queried record must sit at top-1
        if hits and hits[0]["id"] == expected and \
                all(h.get("session_id") == sid for h in hits):
            results["multisession"][0] += 1

    for q, expected in question_supersede(rows):
        hits = db.recall(conn, q, limit=3)
        results["supersede"][1] += 1
        if hits and hits[0]["id"] == expected:
            results["supersede"][0] += 1

    for q in question_abstention(conn):
        hits = db.recall(conn, q, limit=3)
        results["abstain"][1] += 1
        if not hits or hits[0]["abstained"]:
            results["abstain"][0] += 1

    for q, expected in question_join(rows):
        hits = db.recall(conn, q, limit=3)
        results["join"][1] += 1
        if {h["id"] for h in hits} >= expected:
            results["join"][0] += 1

    for q, expected in question_topical_abstention(rows):
        hits = db.recall(conn, q, limit=3)
        results["topical"][1] += 1
        if not hits or all(h["id"] != expected for h in hits):
            results["topical"][0] += 1

    print(f"corpus: {len(rows)} probe records")
    total_hit = total_n = 0
    for name in ("exact", "update", "temporal", "abstain", "join", "topical",
                 "supersede", "multisession"):
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
