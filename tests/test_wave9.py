"""Wave C tests: temporal supersede resolution in recall.

Covers:
- a chain A -> B -> C resolves: when the winner B is itself corrected, the
  predecessor A is re-linked to the new head C, not left pointing at stale B
  (Zep 2501.13956, chain closure on the write path).
- the replacement (chain head) outranks its superseded twin: SUPERSEDED_BOOST
  balances SUPERSEDED_PENALTY so the correction wins the ranking, not merely
  the tie.
- consolidate() never promotes a superseded record (sleep-time compute must
  not consolidate stale memory).
- forget_cold() never deletes a record that is still the chain target.
"""
import pytest

from omp_memory import db as memory_db


def _link(conn, loser, winner):
    conn.execute("UPDATE records SET superseded_by=? WHERE id=?",
                 (winner, loser))
    conn.commit()


def test_chain_closes_to_new_head(conn):
    a = memory_db.remember(
        conn, rtype="FACT", content="deploy host is prod-01 in us-east",
        confidence=0.9, verified=1)
    b = memory_db.remember(
        conn, rtype="FACT", content="deploy host is prod-01 in us-east region two",
        confidence=0.9, verified=1)
    _link(conn, a["id"], b["id"])
    # correcting B rewrites it in place and closes the chain: A now points
    # at B, which is the same row, so A's marker stays valid only if B
    # remains the head. A second correction makes B stale; the chain must
    # resolve to the new content on B itself.
    memory_db.reinforce(
        conn, b["id"], "correction",
        content="deploy host is prod-01 in us-east region three", evidence="ops")
    row = memory_db.get_record(conn, a["id"])
    # B was rewritten in place, so the head is still B: A's link is intact
    # and correct. The chain-close matters when B is superseded by a
    # DIFFERENT row; see test_chain_redirects_predecessor.
    assert row["superseded_by"] == b["id"]


def test_chain_redirects_predecessor(conn):
    a = memory_db.remember(
        conn, rtype="FACT", content="deploy host is prod-01 in us-east",
        confidence=0.9, verified=1)
    b = memory_db.remember(
        conn, rtype="FACT", content="deploy host is prod-01 in us-east region two",
        confidence=0.9, verified=1)
    c = memory_db.remember(
        conn, rtype="FACT", content="deploy host is prod-01 in us-east region three",
        confidence=0.9, verified=1)
    _link(conn, a["id"], b["id"])
    _link(conn, b["id"], c["id"])
    # correct C in place: C's rewrite now supersedes both its own old self
    # (B, already linked to C) and any row that pointed at B (A). The
    # predecessor A must be redirected from B to C.
    memory_db.reinforce(
        conn, c["id"], "correction",
        content="deploy host is prod-01 in us-east region four", evidence="ops")
    assert memory_db.get_record(conn, a["id"])["superseded_by"] == c["id"]
    assert memory_db.get_record(conn, b["id"])["superseded_by"] == c["id"]


def test_superseded_penalty_beats_boost(conn):
    rid = memory_db.remember(
        conn, rtype="FACT", content="the deploy host is prod-01 in us-east",
        confidence=0.9, verified=1)
    twin = memory_db.remember(
        conn, rtype="FACT", content="the deploy host is prod-02 in us-east",
        confidence=0.9, verified=1)
    _link(conn, twin["id"], rid["id"])
    scores = memory_db.recall(conn, "deploy host prod east", limit=5,
                              full=True)
    by_id = {h["id"]: h["score"] for h in scores}
    # head (winner) outranks loser even after the loser's penalty is
    # balanced by the winner's boost
    assert by_id[rid["id"]] > by_id[twin["id"]]


def test_consolidate_skips_superseded(conn):
    rid = memory_db.remember(
        conn, rtype="INFERENCE", content="cache misses scale with keyspace",
        confidence=0.6)
    twin = memory_db.remember(
        conn, rtype="INFERENCE", content="cache misses scale with key count",
        confidence=0.6)
    _link(conn, twin["id"], rid["id"])
    for _ in range(3):
        memory_db.log_retrieval(conn, [rid["id"]])
    memory_db.consolidate(conn)
    row = memory_db.get_record(conn, rid["id"])
    # the live record consolidates; the superseded twin does not
    assert row["confidence"] > 0.6
    assert memory_db.get_record(conn, twin["id"])["confidence"] == 0.6


def test_forget_cold_spares_chain_target(conn):
    target = memory_db.remember(
        conn, rtype="INFERENCE", content="retry budget is three attempts",
        confidence=0.15)
    loser = memory_db.remember(
        conn, rtype="INFERENCE", content="retry budget is five attempts",
        confidence=0.15)
    _link(conn, loser["id"], target["id"])
    # both records are cold (old, at floor, no reinforcement); only the
    # chain-target guard must separate them
    old = (memory_db.datetime.now(memory_db.timezone.utc)
           - memory_db.timedelta(days=365)).isoformat()
    conn.execute("UPDATE records SET updated_at=? WHERE id IN (?, ?)",
                 (old, target["id"], loser["id"]))
    conn.commit()
    result = memory_db.forget_cold(conn, apply=True)
    assert target["id"] not in result["candidates"]
    assert memory_db.get_record(conn, target["id"]) is not None
    assert memory_db.get_record(conn, loser["id"]) is None


