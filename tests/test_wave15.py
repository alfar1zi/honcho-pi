"""Wave I: adoptions from the remembrane code audit.

- conflicts() flags numeric mismatch between otherwise-similar records
  (deterministic, no LLM; remembrane conflicts.py pattern).
- digest() refills the char budget with shorter records after a long one
  no longer fits, instead of stopping at the first overflow.
- web graph cache invalidates on PRAGMA data_version so writes from the
  timer or MCP (other processes) surface without waiting for a UI edit.
"""
import json

from omp_memory import db


def test_conflicts_flags_numeric_mismatch(conn):
    a = db.remember(conn, "FACT", "gateway timeout budget is 250 ms under load",
                    evidence="load test 1", verified=1, source="s1")
    b = db.remember(conn, "FACT", "gateway timeout budget is 3000 ms under load",
                    evidence="load test 2", source="s1")
    pairs = db.conflicts(conn, [a["id"], b["id"]])
    assert any(p["kind"] == "numeric" for p in pairs), pairs


def test_conflicts_numeric_ignores_unrelated_records(conn):
    a = db.remember(conn, "FACT", "gateway timeout budget is 250 ms under load",
                    evidence="load test 1", verified=1, source="s1")
    b = db.remember(conn, "FACT", "office plants rotation schedule is weekly",
                    evidence="calendar", source="s1")
    pairs = db.conflicts(conn, [a["id"], b["id"]])
    assert not pairs


def test_conflicts_same_numbers_no_flag(conn):
    a = db.remember(conn, "FACT", "gateway timeout budget is 250 ms under load",
                    evidence="load test 1", verified=1, source="s1")
    b = db.remember(conn, "FACT", "gateway timeout budget stays 250 ms under load",
                    evidence="load test 2", source="s1")
    pairs = db.conflicts(conn, [a["id"], b["id"]])
    assert not any(p["kind"] == "numeric" for p in pairs)


def test_digest_refills_budget_after_long_record(conn):
    # Both lines are ~155 chars; the budget fits the long one plus the
    # short one only if overflow skips instead of stopping the loop.
    db.remember(conn, "RULE", "x" * 100 + " long rule content here",
                evidence="e", verified=1, source="s1")
    db.remember(conn, "RULE", "tiny rule", evidence="e", verified=1, source="s1")
    d = db.digest(conn, budget=220)
    assert d["records"] == 2, d["text"]
    assert "tiny rule" in d["text"]
    assert d["chars"] <= d["budget"]


def test_digest_never_exceeds_budget(conn):
    for i in range(10):
        db.remember(conn, "RULE", f"rule number {i} " + "y" * (i * 13),
                    evidence="e", verified=1, source="s1")
    d = db.digest(conn, budget=300)
    assert d["chars"] <= 300


def test_store_version_tracks_other_process_writes(conn, db_path):
    v1 = db.store_version(conn)
    other = db.connect(db_path)
    try:
        db.remember(other, "FACT", "a fact from another process", evidence="e", source="s1")
        other.commit()
        assert db.store_version(conn) != v1
        assert db.store_version(conn) == db.store_version(conn)  # stable without writes
    finally:
        other.close()