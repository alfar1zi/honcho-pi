"""Mem0-style taxonomy in decide(): update, create, delete, noop.

A repeated fact is not always a write. A restatement of a discredited
fact should delete; a restatement of a fully confirmed fact should do
nothing; everything else keeps the old create/update contract.
"""
from datetime import datetime, timedelta, timezone

from omp_memory import db as memory_db


def _discredit(conn, rid):
    memory_db.reinforce(conn, rid, "failure", source="ops")


def test_decide_delete_on_discredited_duplicate(conn):
    rid = memory_db.remember(conn, "FACT", "the port is 8080",
                             confidence=0.6)["id"]
    _discredit(conn, rid)
    decision = memory_db.decide(conn, "the port is 8080")
    assert decision["action"] == "delete"
    assert decision["id"] == rid


def test_decide_noop_on_fully_confirmed_fact(conn):
    rid = memory_db.remember(conn, "FACT", "auth uses magic links",
                             confidence=0.96, verified=1)["id"]
    decision = memory_db.decide(conn, "auth uses magic links")
    assert decision["action"] == "noop"
    assert decision["id"] == rid


def test_decide_update_still_fires_on_unverified_duplicate(conn):
    rid = memory_db.remember(conn, "FACT", "the port is 8080",
                             confidence=0.6)["id"]
    decision = memory_db.decide(conn, "the port is 8080")
    assert decision["action"] == "update"
    assert decision["id"] == rid


def test_decide_delete_requires_unverified(conn):
    # a verified record with a failure signal is still confirmed state
    rid = memory_db.remember(conn, "FACT", "the port is 8080",
                             confidence=0.6, verified=1)["id"]
    _discredit(conn, rid)
    decision = memory_db.decide(conn, "the port is 8080")
    assert decision["action"] == "update"


def test_decide_never_writes(conn):
    rid = memory_db.remember(conn, "FACT", "the port is 8080",
                             confidence=0.6)["id"]
    _discredit(conn, rid)
    before = memory_db.stats(conn)
    memory_db.decide(conn, "the port is 8080")
    memory_db.decide(conn, "auth uses magic links")
    after = memory_db.stats(conn)
    assert before == after
