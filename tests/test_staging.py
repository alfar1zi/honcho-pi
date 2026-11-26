"""The staging pipeline: a write waits for approval instead of landing.

This is the safety path that stops an agent from writing memory on its own.
It had no test, so a regression here would ship silently.
"""
import json

import pytest

from omp_memory import db, mcp


def _stage(conn, content="staged candidate about caching layer", project="p1"):
    return db.stage_write(conn, {
        "rtype": "FACT", "content": content, "evidence": "probe",
        "confidence": 0.4, "source": "probe", "tags": "",
        "session_id": "", "project_id": project, "event_id": "",
    })


def test_pending_lists_the_staged_candidate(conn):
    _stage(conn)
    rows = db.list_pending(conn)
    assert len(rows) == 1
    assert rows[0]["content"].startswith("staged candidate")
    assert rows[0]["status"] == "pending"


def test_approve_writes_the_record(conn):
    pid = _stage(conn)
    out = db.decide_pending(conn, pid, approve=True)
    assert out["status"] == "approved"
    assert out["record_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM records WHERE content LIKE 'staged candidate%'"
    ).fetchone()[0] == 1
    assert db.list_pending(conn) == []


def test_reject_writes_nothing(conn):
    pid = _stage(conn, "candidate that must not land")
    out = db.decide_pending(conn, pid, approve=False)
    assert out["status"] == "rejected"
    assert conn.execute(
        "SELECT COUNT(*) FROM records WHERE content LIKE 'candidate that must not%'"
    ).fetchone()[0] == 0


def test_staging_refuses_a_changed_payload(conn):
    """The content hash pins what was reviewed."""
    pid = _stage(conn, "original reviewed content about caches")
    conn.execute("UPDATE pending_writes SET content=? WHERE id=?",
                 ("swapped content the reviewer never saw", pid))
    conn.commit()
    # The pin raises rather than returning a status: a reviewer approving a
    # payload that changed under them must not get a silent "ok".
    with pytest.raises(ValueError, match="changed since staging"):
        db.decide_pending(conn, pid, approve=True)
    assert conn.execute(
        "SELECT COUNT(*) FROM records WHERE content LIKE 'swapped content%'"
    ).fetchone()[0] == 0


def test_duplicate_pending_content_is_not_staged_twice(conn):
    a = _stage(conn, "same candidate twice")
    b = _stage(conn, "same candidate twice")
    assert a == b
    assert len(db.list_pending(conn)) == 1


def test_mcp_maintenance_pending_and_approve_round_trip(conn):
    pid = _stage(conn, "through the mcp surface")
    listed = json.loads(mcp.tool_maintenance({"op": "pending"}))
    assert listed and listed[0]["id"] == pid
    out = json.loads(mcp.tool_maintenance({"op": "approve", "pending_id": pid}))
    assert out["status"] == "approved"


def test_not_conflict_is_remembered(conn):
    a = db.remember(conn, "FACT", "cache ttl is sixty seconds")["id"]
    b = db.remember(conn, "FACT", "cache ttl is ninety seconds")["id"]
    db.mark_not_conflict(conn, a, b)
    # conflicts() takes the candidate ids to inspect, not a limit.
    again = db.conflicts(conn, [a, b])
    assert all({r.get("a"), r.get("b")} != {a, b} for r in again)
