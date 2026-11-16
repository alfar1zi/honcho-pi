"""Wave D tests: WORKFLOW record type.

A WORKFLOW is a step-by-step procedure the operator confirmed working,
distinct from a DECISION (why) and a FACT (what): the workflow is the how.
Covers:
- roundtrip: remembered, recalled by type filter.
- ranking: WORKFLOW prior sits between CORRECTION and INTENT, so on a
  lexical tie the binding type wins and the routine type loses.
- sleep-time: promote_hot never promotes a WORKFLOW (routine procedure is
  not inference that deserves verified status).
- migration: a pre-v13 CHECK (no WORKFLOW) is rebuilt on init_db and the
  type becomes insertable without losing existing rows.
"""
import pytest

from omp_memory import db as memory_db


def test_workflow_roundtrip_with_type_filter(conn):
    memory_db.remember(
        conn, rtype="WORKFLOW",
        content="release: run make test, then tag v, then push tag",
        confidence=0.9)
    memory_db.remember(
        conn, rtype="FACT",
        content="release: run make test, then tag v, then push tag",
        confidence=0.9)
    hits = memory_db.recall(conn, "release tag push", rtype="WORKFLOW", full=True)
    assert hits and all(h["type"] == "WORKFLOW" for h in hits)


def test_workflow_prior_ranks_between_correction_and_intent(conn):
    base = "deploy: build image, push to registry, roll out manifest"
    memory_db.remember(
        conn, rtype="CORRECTION", content=base + " via blue-green", confidence=0.8)
    memory_db.remember(
        conn, rtype="WORKFLOW", content=base + " with canary", confidence=0.8)
    memory_db.remember(
        conn, rtype="INTENT", content=base + " this sprint", confidence=0.8)
    hits = memory_db.recall(conn, "deploy build image push registry", limit=5)
    types = [h["type"] for h in hits]
    assert types.index("CORRECTION") < types.index("WORKFLOW") < types.index("INTENT")


def test_workflow_not_promotable(conn):
    r = memory_db.remember(
        conn, rtype="WORKFLOW",
        content="rotate keys: generate, stage, verify, activate",
        confidence=0.99)
    # push heat over PROMOTE_HEAT so any promotion would be visible
    memory_db.reinforce(conn, r["id"], "reinforce")
    memory_db.reinforce(conn, r["id"], "reinforce")
    memory_db.promote_hot(conn)
    row = memory_db.get_record(conn, r["id"])
    assert row["verified"] == 0


def test_workflow_check_constraint_rebuild(conn):
    # simulate a pre-v13 database: the CHECK lacks WORKFLOW but the
    # schema version says 12, so the v10 block is skipped and only the
    # v13 migration sees the stale CHECK.
    conn.execute("DROP TABLE records")
    conn.execute("""
        CREATE TABLE records (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL CHECK (type IN
                ('FACT','ASSUMPTION','INFERENCE','DECISION','RULE',
                 'CORRECTION','INTENT')),
            content TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            verified INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            parent_id TEXT NOT NULL DEFAULT '',
            project_ids TEXT NOT NULL DEFAULT '',
            last_decay_at TEXT NOT NULL DEFAULT '',
            about_peer TEXT NOT NULL DEFAULT '',
            event_id TEXT NOT NULL DEFAULT '',
            decay_exempt_until TEXT NOT NULL DEFAULT '',
            superseded_by TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 0.5,
            cue TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (12)")
    conn.execute(
        "INSERT INTO records (id,type,content,created_at,updated_at)"
        " VALUES ('legacy','FACT','old fact','t','t')")
    conn.commit()
    memory_db.init_db(conn)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
    ).fetchone()[0]
    assert "'WORKFLOW'" in sql
    assert conn.execute(
        "SELECT content FROM records WHERE id='legacy'").fetchone()[0] == "old fact"
    r = memory_db.remember(
        conn, rtype="WORKFLOW",
        content="restore: stop service, swap db, start service",
        confidence=0.9)
    assert r["type"] == "WORKFLOW"
