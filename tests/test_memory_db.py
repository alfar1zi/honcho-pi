"""Regression tests for critical defects found in audit.

Each test names the defect it locks down. Run BEFORE the fix to see RED.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from omp_memory import db as memory_db

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- Defect 1: connect() deletes the DB when open fails ---------------------
# A locked/corrupt-handle DB must not be unlinked; data loss is silent.

def test_connect_does_not_delete_db_on_failure(db_path, monkeypatch):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"not a sqlite db at all")
    before = db_path.read_bytes()

    real_connect = sqlite3.connect

    class FailingConn:
        """Proxy that opens fine but fails on the first PRAGMA."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if "journal_mode" in sql:
                raise sqlite3.DatabaseError("disk I/O error")
            return self._conn.execute(sql, *args, **kwargs)

        def close(self):
            self._conn.close()

    def failing_connect(*a, **kw):
        return FailingConn(real_connect(*a, **kw))

    monkeypatch.setattr(memory_db.sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.DatabaseError):
        memory_db.connect(str(db_path))

    assert db_path.exists(), "connect() deleted a DB it failed to open"
    assert db_path.read_bytes() == before, "connect() mutated a DB it failed to open"


# --- Defect 2: backup_db is a raw file copy, not a consistent snapshot ------
# shutil.copy2 of a live WAL DB can capture a torn state.

def test_backup_is_consistent_snapshot(conn, db_path, monkeypatch):
    monkeypatch.setattr(memory_db, "BACKUP_DIR", str(db_path.parent / "backups"))
    memory_db.upsert_record(conn, "FACT", "lesson one", session_id="s1")
    memory_db.upsert_record(conn, "FACT", "lesson two", session_id="s2")

    bak = memory_db.backup_db(str(db_path))
    assert bak and os.path.exists(bak)

    # the backup must be a readable, complete DB - not a torn copy
    b = sqlite3.connect(bak)
    try:
        rows = b.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        b.close()
    assert rows == 2, f"backup snapshot incomplete: {rows} records"


def test_backup_does_not_collide_within_same_second(conn, db_path, monkeypatch):
    monkeypatch.setattr(memory_db, "BACKUP_DIR", str(db_path.parent / "backups"))
    memory_db.upsert_record(conn, "FACT", "collision probe", session_id="s1")

    first = memory_db.backup_db(str(db_path))
    second = memory_db.backup_db(str(db_path))
    assert first != second, "two backups in the same second overwrite each other"
    assert os.path.exists(first) and os.path.exists(second)


# --- Defect 3: repair_from_backup passes a path where a connection is needed -

def test_repair_from_backup_returns_bool_and_restores(conn, db_path, monkeypatch):
    monkeypatch.setattr(memory_db, "BACKUP_DIR", str(db_path.parent / "backups"))
    memory_db.upsert_record(conn, "FACT", "pre-corruption lesson", session_id="s1")
    bak = memory_db.backup_db(str(db_path))
    assert bak

    # corrupt the live DB
    conn.close()
    db_path.write_bytes(b"garbage")

    result = memory_db.repair_from_backup(str(db_path))
    assert isinstance(result, bool), "repair_from_backup must return a bool, not a connection"
    assert result is True, "repair_from_backup failed to restore from a valid backup"

    c = memory_db.connect(str(db_path))
    try:
        rows = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        c.close()
    assert rows == 1, "repair restored the file but lost records"


# --- Defect 4: upsert_record binds a tuple as a parameter -------------------

def test_upsert_record_persists_all_fields(conn):
    rid = memory_db.upsert_record(
        conn,
        rtype="DECISION",
        content="use sqlite not postgres",
        evidence="benchmark 2026-10",
        verified=1,
        confidence=0.9,
        source="council",
        tags="db,choice",
        session_id="sess-1",
        project_id="proj-1",
        parent_id="",
    )
    rec = memory_db.get_record(conn, rid)
    assert rec is not None
    assert rec["type"] == "DECISION"
    assert rec["content"] == "use sqlite not postgres"
    assert rec["evidence"] == "benchmark 2026-10"
    assert rec["verified"] == 1
    assert rec["confidence"] == 0.9
    assert rec["source"] == "council"
    assert rec["tags"] == "db,choice"
    assert rec["session_id"] == "sess-1"
    assert rec["project_id"] == "proj-1"


def test_upsert_same_content_same_source_same_project_dedups(conn):
    a = memory_db.upsert_record(conn, "FACT", "same lesson", session_id="s", project_id="p")
    b = memory_db.upsert_record(conn, "FACT", "same lesson", session_id="s", project_id="p")
    assert a == b, "identical content+source+project must produce the same record id"
    rows = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert rows == 1


def test_upsert_different_project_is_distinct(conn):
    a = memory_db.upsert_record(conn, "FACT", "same lesson", session_id="s", project_id="p1")
    b = memory_db.upsert_record(conn, "FACT", "same lesson", session_id="s", project_id="p2")
    assert a != b, "same content in different projects must not collapse"


# --- Defect 5: search() crashes on FTS operator characters ------------------

@pytest.mark.parametrize("query", ['a"b', "a AND", "OR", "a*", "-x", "a(b)", "NEAR(", "^x", 'a"b OR c'])
def test_search_survives_fts_operator_input(conn, query):
    memory_db.upsert_record(conn, "FACT", "alpha beta gamma", session_id="s")
    results = memory_db.search(conn, query)
    assert isinstance(results, list), f"search({query!r}) must return a list, not raise"


def test_search_returns_matching_record(conn):
    memory_db.upsert_record(conn, "FACT", "wal mode prevents corruption", session_id="s")
    results = memory_db.search(conn, "wal")
    assert any("wal mode" in r["content"] for r in results)


def test_search_respects_project_filter(conn):
    memory_db.upsert_record(conn, "FACT", "shared keyword lesson", session_id="s", project_id="p1")
    memory_db.upsert_record(conn, "FACT", "shared keyword lesson", session_id="s", project_id="p2")
    results = memory_db.search(conn, "shared", project_id="p1")
    assert len(results) == 1
    assert results[0]["project_id"] == "p1"


# --- Defect 6: FTS index can desync from the records table ------------------

def test_fts_stays_in_sync_after_upsert(conn):
    rid = memory_db.upsert_record(conn, "FACT", "original content zzz",
                                  evidence="first", session_id="s", source="session-capture")
    # same identity, new evidence: must update in place, not insert a second row
    rid2 = memory_db.upsert_record(
        conn, rtype="FACT", content="original content zzz", evidence="updated",
        session_id="s", project_id="", source="session-capture",
    )
    assert rid2 == rid, "same content+source+project must update the same record"
    hits = memory_db.search(conn, "zzz")
    assert len(hits) == 1, f"upsert created a duplicate row: {len(hits)}"
    stale = memory_db.search(conn, "updated")
    assert any(r["id"] == rid for r in stale), "FTS did not index the updated evidence column"
    gone = memory_db.search(conn, "first")
    assert not any(r["id"] == rid for r in gone), "FTS kept the stale evidence text"


# --- Defect 7: init_db is not idempotent across schema changes ---------------

def test_init_db_is_idempotent(conn, db_path):
    memory_db.upsert_record(conn, "FACT", "persist me", session_id="s")
    c2 = memory_db.connect(str(db_path))
    try:
        memory_db.init_db(c2)  # must not raise or wipe
        rows = c2.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        c2.close()
    assert rows == 1, "re-running init_db destroyed existing records"


# --- Defect 8: ensure_dirs ignores a reconfigured DB_PATH --------------------

def test_ensure_dirs_uses_configured_paths(tmp_path, monkeypatch):
    custom = tmp_path / "custom" / "memory.db"
    monkeypatch.setattr(memory_db, "DB_PATH", str(custom))
    monkeypatch.setattr(memory_db, "BACKUP_DIR", str(tmp_path / "custom" / "backups"))
    memory_db.ensure_dirs()
    assert custom.parent.is_dir(), "ensure_dirs created the hardcoded default dir, not DB_PATH's parent"
    assert (tmp_path / "custom" / "backups").is_dir()


# --- Defect 9: health() reports the configured DB path -----------------------

def test_health_reports_configured_db_path(conn, db_path, monkeypatch):
    monkeypatch.setattr(memory_db, "DB_PATH", str(db_path))
    h = memory_db.health(conn)
    assert h["db_path"] == str(db_path), "health() reports the import-time default path"


# --- Defect 10: reinforce() clamps confidence into [0, 1] --------------------

def test_reinforce_clamps_confidence(conn):
    rid = memory_db.upsert_record(conn, "FACT", "clamp probe", session_id="s", confidence=0.95)
    for _ in range(10):
        memory_db.reinforce(conn, rid, "success")
    rec = memory_db.get_record(conn, rid)
    assert rec["confidence"] <= 1.0, f"confidence exceeded 1.0: {rec['confidence']}"

    rid2 = memory_db.upsert_record(conn, "FACT", "clamp probe 2", session_id="s", confidence=0.05)
    for _ in range(10):
        memory_db.reinforce(conn, rid2, "failure")
    rec2 = memory_db.get_record(conn, rid2)
    assert rec2["confidence"] >= 0.0, f"confidence went negative: {rec2['confidence']}"


def test_reinforce_rejects_unknown_signal(conn):
    rid = memory_db.upsert_record(conn, "FACT", "signal probe", session_id="s")
    with pytest.raises(ValueError):
        memory_db.reinforce(conn, rid, "not-a-signal")


# --- Defect 11: invalid record type must be rejected -------------------------

def test_upsert_rejects_invalid_type(conn):
    with pytest.raises(ValueError):
        memory_db.upsert_record(conn, "NOT_A_TYPE", "x", session_id="s")


# --- Defect 12: session lineage + checkpoint round-trip ----------------------

def test_session_lineage_round_trip(conn):
    parent = memory_db.start_session(conn, session_id="parent-1", project_id="p")
    child = memory_db.start_session(conn, session_id="child-1", project_id="p", parent_id="parent-1")
    assert parent == "parent-1"
    assert child == "child-1"
    memory_db.end_session(conn, "child-1", summary="did the thing")
    sessions = {s["id"]: s for s in memory_db.list_sessions(conn)}
    assert sessions["child-1"]["parent_id"] == "parent-1", "parent lineage not persisted"
    assert sessions["child-1"]["ended_at"], "end_session did not stamp ended_at"
    assert sessions["child-1"]["summary"] == "did the thing"


def test_start_session_is_idempotent(conn):
    memory_db.start_session(conn, session_id="s-1", project_id="p")
    memory_db.start_session(conn, session_id="s-1", project_id="p")
    rows = [s for s in memory_db.list_sessions(conn) if s["id"] == "s-1"]
    assert len(rows) == 1, "re-starting a session duplicated the row"


def test_pre_compress_checkpoint_round_trip(conn):
    memory_db.upsert_record(conn, "FACT", "checkpoint payload", session_id="s", project_id="p")
    cp = memory_db.create_pre_compress_checkpoint(conn, session_id="s", project_id="p")
    assert cp["api_version"] == memory_db.PRE_COMPRESS_CHECKPOINT_API_VERSION
    assert cp["record_count"] == 1

    # wipe the live rows, then restore purely from the checkpoint payload
    conn.execute("DELETE FROM records")
    conn.commit()
    assert memory_db.search(conn, "checkpoint payload") == []

    restored = memory_db.restore_from_checkpoint(conn, cp["id"])
    assert restored["restored_records"] == 1, "restore did not write the payload back"
    hits = memory_db.search(conn, "checkpoint payload")
    assert hits, "checkpoint restore lost the record"
    assert hits[0]["session_id"] == "s"


def test_restore_from_checkpoint_is_idempotent(conn):
    memory_db.upsert_record(conn, "FACT", "idempotent payload", session_id="s", project_id="p")
    cp = memory_db.create_pre_compress_checkpoint(conn, session_id="s", project_id="p")
    first = memory_db.restore_from_checkpoint(conn, cp["id"])
    second = memory_db.restore_from_checkpoint(conn, cp["id"])
    assert first["restored_records"] == second["restored_records"] == 1
    rows = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert rows == 1, f"double restore duplicated rows: {rows}"


def test_restore_unknown_checkpoint_raises(conn):
    with pytest.raises(ValueError):
        memory_db.restore_from_checkpoint(conn, "does-not-exist")


# --- Defect 13: profile isolation --------------------------------------------

def test_profile_context_is_isolated(conn):
    memory_db.set_profile_context(conn, "work", {"key": "value"})
    assert memory_db.get_profile_context(conn, "work") == {"key": "value"}
    assert memory_db.get_profile_context(conn, "other") is None
    memory_db.clear_profile()


# --- Portability: a failing chmod must not abort a backup or a repair ------

def test_backup_survives_chmod_failure(conn, db_path, monkeypatch):
    """Windows chmod only toggles the read-only bit and can raise; a backup is still valid."""
    monkeypatch.setattr(memory_db, "BACKUP_DIR", str(db_path.parent / "backups"))
    memory_db.upsert_record(conn, "FACT", "portable lesson", session_id="s1")

    def _boom(path, mode):
        raise OSError("chmod not supported here")

    monkeypatch.setattr(memory_db.os, "chmod", _boom)
    bak = memory_db.backup_db(str(db_path))
    assert bak and os.path.exists(bak), "a chmod failure aborted the backup"


def test_restrict_to_owner_is_a_noop_when_chmod_fails(tmp_path, monkeypatch):
    target = tmp_path / "probe.txt"
    target.write_text("x", encoding="utf-8")

    def _boom(path, mode):
        raise NotImplementedError

    monkeypatch.setattr(memory_db.os, "chmod", _boom)
    memory_db._restrict_to_owner(str(target))


# --- list_records: full table read for the overview graph -------------------

def test_list_records_returns_all(conn):
    memory_db.upsert_record(conn, "FACT", "first lesson", session_id="s")
    memory_db.upsert_record(conn, "RULE", "second lesson", session_id="s")
    rows = memory_db.list_records(conn)
    assert len(rows) == 2
    assert {r["content"] for r in rows} == {"first lesson", "second lesson"}


def test_list_records_orders_newest_first(conn):
    memory_db.upsert_record(conn, "FACT", "old lesson", session_id="s")
    memory_db.upsert_record(conn, "FACT", "new lesson", session_id="s")
    rows = memory_db.list_records(conn)
    assert rows[0]["content"] == "new lesson"


def test_list_records_respects_limit(conn):
    for i in range(5):
        memory_db.upsert_record(conn, "FACT", f"lesson {i}", session_id="s")
    assert len(memory_db.list_records(conn, limit=3)) == 3


def test_schema_prior_coherent_record_starts_higher(conn):
    """Tse et al. 2007: agreement with trusted memory raises the prior."""
    trusted = memory_db.remember(conn, "FACT", "deploy uses docker compose",
                                 confidence=0.9, source="seed")["id"]
    conn.execute("UPDATE records SET verified=1 WHERE id=?", (trusted,))
    conn.commit()
    out = memory_db.remember(conn, "FACT", "deploy uses docker compose files",
                             confidence=0.5, source="agent")
    assert out["applied"] == "inserted"
    new_conf = memory_db.get_record(conn, out["id"])["confidence"]
    assert new_conf > 0.5, "coherent-with-schema record must start above the caller's prior"
    assert new_conf <= 0.5 + memory_db.SCHEMA_PRIOR_WEIGHT + 1e-9


def test_schema_prior_contradictory_starts_lower(conn):
    """A record nothing trusted agrees with keeps the caller's prior."""
    out = memory_db.remember(conn, "FACT", "zebra migration crosses kenya",
                             confidence=0.5, source="agent")
    assert memory_db.get_record(conn, out["id"])["confidence"] == 0.5


def test_rif_correction_penalizes_superseded_record(conn):
    """Anderson et al. 1994: a correction inhibits the record it supersedes."""
    old = memory_db.remember(conn, "FACT", "deploy runs on kubernetes",
                             confidence=0.9, source="seed")
    correction = memory_db.remember(conn, "CORRECTION",
                                    "deploy runs on nomad, not kubernetes",
                                    confidence=0.8, source="agent")
    rif = correction["rif"]
    assert len(rif) == 1
    assert rif[0]["id"] == old["id"]
    assert rif[0]["to"] == pytest.approx(0.75)
    assert memory_db.get_record(conn, old["id"])["confidence"] == pytest.approx(0.75)
    # the penalty is logged as an ordinary failure signal
    n = conn.execute("SELECT count(*) FROM reinforcement WHERE record_id=?"
                     " AND source='rif'", (old["id"],)).fetchone()[0]
    assert n == 1


def test_rif_ignores_low_confidence_losers(conn):
    """A loser already near the floor gains nothing from extra inhibition."""
    memory_db.remember(conn, "FACT", "deploy runs on kubernetes",
                       confidence=0.4, source="seed")
    correction = memory_db.remember(conn, "CORRECTION",
                                    "deploy runs on nomad, not kubernetes",
                                    confidence=0.8, source="agent")
    assert correction["rif"] == []
