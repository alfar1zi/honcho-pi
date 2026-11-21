"""Wave J: graphiti/engram adoptions (code audit 2026-11-21).

- Future-tense guard: a plan ("will rotate", "akan ganti") is not a fact
  valid from now; valid_from defaults to the statement time only when the
  caller passes nothing, and planned wording is flagged in the advisory.
- MMR diversification over term overlap: near-duplicate hits stop eating
  the whole recall window (graphiti search_utils.py:1670 pattern, stdlib).
- not_conflict persistence: an adjudicated pair never resurfaces
  (engram relations.go pattern).
"""
from omp_memory import db


def test_mmr_diversifies_near_duplicate_hits(conn):
    db.remember(conn, "FACT", "wal mode fixes the database lock timeout",
                evidence="e1", verified=1, source="s1")
    db.remember(conn, "FACT", "wal mode fixes database lock timeouts reliably",
                evidence="e2", verified=1, source="s1")
    db.remember(conn, "FACT", "vacuum after bulk delete reclaims database space",
                evidence="e3", verified=1, source="s1")
    hits = db.recall(conn, "database wal lock vacuum", limit=3, full=True)
    contents = [h["content"] for h in hits]
    assert len(hits) == 3
    # The two WAL twins must not both outrank the distinct vacuum lesson.
    wal_positions = [i for i, c in enumerate(contents) if "wal mode" in c]
    vac_positions = [i for i, c in enumerate(contents) if "vacuum" in c]
    assert vac_positions and min(vac_positions) < max(wal_positions)


def test_mmr_keeps_top_hit_first(conn):
    db.remember(conn, "RULE", "always checkpoint before upgrade",
                evidence="e", verified=1, source="s1")
    db.remember(conn, "FACT", "checkpoint history lives in the ops log",
                evidence="e", source="s1")
    hits = db.recall(conn, "checkpoint before upgrade", limit=2, full=True)
    assert hits[0]["type"] == "RULE"


def test_not_conflict_pair_stays_silent(conn):
    a = db.remember(conn, "FACT", "gateway timeout budget is 250 ms under load",
                    evidence="e", verified=1, source="s1")
    b = db.remember(conn, "FACT", "gateway timeout budget is 3000 ms under load",
                    evidence="e", source="s1")
    assert db.conflicts(conn, [a["id"], b["id"]])
    db.mark_not_conflict(conn, a["id"], b["id"])
    assert db.conflicts(conn, [a["id"], b["id"]]) == []
    # order-independent
    db.mark_not_conflict(conn, b["id"], a["id"])
    assert db.conflicts(conn, [a["id"], b["id"]]) == []


def test_future_tense_guard_flags_plans(conn):
    out = db.remember(conn, "FACT", "we will rotate the gateway token next sprint",
                      evidence="planning meeting", source="s1",
                      valid_from="2026-12-01T00:00:00+00:00")
    assert out.get("advisory") and "plan" in out["advisory"].lower()


def test_past_fact_no_flag(conn):
    out = db.remember(conn, "FACT", "the gateway token rotates every week",
                      evidence="runbook", verified=1, source="s1",
                      valid_from="2026-01-01T00:00:00+00:00")
    assert not out.get("advisory") or "plan" not in (out["advisory"] or "").lower()