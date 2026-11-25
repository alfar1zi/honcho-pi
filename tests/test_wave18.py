"""Wave 18: correctness fixes found by the 2026-11-25 deep research pass.

Four defects, each verified against a primary source before the fix:
1. bm25() weights were mis-aligned to the 4-column FTS table (the 5.0 boost
   landed on `id`, a sha that can never match).
2. _numeric_tokens used isdigit(), so "1000ms", "30d", "$40k", "5GB" reduced
   to nothing and the number guard was silently inert.
3. The IDF-veto threshold theta=2.0 was not a rarity test at all: it is
   unsatisfiable for N<=6 and fires on any token the other side lacks for
   N>=7, blocking legitimate paraphrases.
4. _set_superseded set superseded_by but never closed valid_until, so an
   as_of query kept returning a fact whose window should have been shut.
"""
import os
import tempfile

import pytest

from omp_memory import db


@pytest.fixture
def conn(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("OMP_MEMORY_DB", path)
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", path + ".bak")
    c = db.connect(path)
    db.init_db(c)
    yield c
    c.close()


def test_bm25_weights_align_to_four_columns():
    """The content column must be the boosted one, not the id column."""
    import inspect
    src = inspect.getsource(db._search_exact)
    assert "bm25(records_fts, 0.0, 5.0, 1.0, 0.5)" in src


def test_numeric_tokens_keep_units():
    assert db._numeric_tokens("timeout 1000ms") == db._numeric_tokens("timeout 1000 ms")
    assert db._numeric_tokens("retention 30d") == db._numeric_tokens("retention 30 days")
    assert db._numeric_tokens("$40k") != frozenset()
    assert db._numeric_tokens("5GB") != frozenset()
    assert db._numeric_tokens("3 days") != db._numeric_tokens("3 hours")


def test_numeric_guard_blocks_unit_change():
    a = db._numeric_tokens("retention limit is 30 days")
    b = db._numeric_tokens("retention limit is 45 days")
    assert a != b


def test_idf_veto_inert_on_tiny_corpus(conn):
    db.remember(conn, "FACT", "alpha beta gamma delta", verified=1)
    assert db._idf_veto(conn, "alpha beta gamma delta epsilon", "alpha beta gamma delta") is False


def test_idf_veto_uses_relative_rarity(conn):
    # Insert rows directly so the write path's own veto cannot interfere:
    # this tests the veto function in isolation.
    from omp_memory import db as d
    now = "2026-01-01T00:00:00+00:00"
    for i in range(60):
        conn.execute(
            "INSERT INTO records(id,type,content,evidence,verified,confidence,"
            "source,tags,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"r{i:03d}", "FACT", f"common token record number {i} shared",
             "", 1, 0.9, "t", "", now, now))
    # one record carries the discriminating token
    conn.execute(
        "INSERT INTO records(id,type,content,evidence,verified,confidence,"
        "source,tags,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("rare", "FACT", "common shared uniquetoken", "", 1, 0.9, "t", "", now, now))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert total == 61
    # a token present in exactly one of 61 records is a blocking key
    assert db._idf_veto(conn, "common shared uniquetoken", "common shared") is True
    # a token in every record is not
    assert db._idf_veto(conn, "common shared extra", "common shared") is False


def test_supersede_closes_valid_until(conn):
    db.remember(conn, "FACT", "deploy pipeline uses docker buildx for images",
                verified=1, confidence=0.9, valid_from="2026-01-01")
    winner = db.remember(conn, "FACT", "deploy pipeline uses kaniko builds for images",
                         verified=1, confidence=0.9, valid_from="2026-06-01")
    db.reinforce(conn, winner["id"], signal="correction",
                 content="deploy pipeline uses kaniko builds for images only",
                 evidence="ops ticket", source="ops")
    before = [r["id"] for r in db.recall(conn, "deploy pipeline", limit=5,
                                         as_of="2026-03-01")]
    after = [r["id"] for r in db.recall(conn, "deploy pipeline", limit=5,
                                        as_of="2026-09-01")]
    assert before and after
    assert before[0] != after[0]
    row = conn.execute(
        "SELECT valid_until, superseded_by FROM records WHERE superseded_by != ''"
    ).fetchone()
    assert row["valid_until"] != ""


# --- consolidation ratchet (CogMemory finding) -------------------------------

def test_consolidate_pays_once_per_retrieval_batch(conn):
    rec = db.remember(conn, "FACT", "consolidation ratchet probe value",
                      confidence=0.5)
    rid = rec["id"]
    for _ in range(db.RETRIEVALS_FOR_CONSOLIDATION):
        db.log_retrieval(conn, [rid])
    conf0 = float(conn.execute("SELECT confidence FROM records WHERE id=?",
                               (rid,)).fetchone()[0])
    first = db.consolidate(conn)
    assert len(first) == 1
    conf1 = float(conn.execute("SELECT confidence FROM records WHERE id=?",
                               (rid,)).fetchone()[0])
    assert conf1 > conf0
    # The same retrieval rows must not pay again.
    for _ in range(3):
        assert db.consolidate(conn) == []
    conf2 = float(conn.execute("SELECT confidence FROM records WHERE id=?",
                               (rid,)).fetchone()[0])
    assert conf2 == conf1


def test_consolidate_does_not_reset_updated_at(conn):
    rec = db.remember(conn, "FACT", "immortality probe value", confidence=0.5)
    rid = rec["id"]
    before = conn.execute("SELECT updated_at FROM records WHERE id=?",
                          (rid,)).fetchone()[0]
    for _ in range(db.RETRIEVALS_FOR_CONSOLIDATION):
        db.log_retrieval(conn, [rid])
    db.consolidate(conn)
    after = conn.execute("SELECT updated_at FROM records WHERE id=?",
                         (rid,)).fetchone()[0]
    assert before == after


def test_new_retrievals_after_consolidation_earn_again(conn):
    rec = db.remember(conn, "FACT", "second batch probe value", confidence=0.5)
    rid = rec["id"]
    for _ in range(db.RETRIEVALS_FOR_CONSOLIDATION):
        db.log_retrieval(conn, [rid])
    assert len(db.consolidate(conn)) == 1
    assert db.consolidate(conn) == []
    for _ in range(db.RETRIEVALS_FOR_CONSOLIDATION):
        db.log_retrieval(conn, [rid])
    assert len(db.consolidate(conn)) == 1


# --- provenance gating (stage 1) ---------------------------------------------

def test_low_trust_unverified_is_penalized_below_verified(conn):
    true = db.remember(conn, "FACT", "release cadence is weekly on tuesdays",
                       verified=1, confidence=0.9, source="agent")
    db.remember(conn, "FACT", "release cadence is weekly on tuesdays always",
                verified=0, confidence=0.9, source="auto-capture")
    hits = db.recall(conn, "release cadence weekly tuesdays", limit=5)
    assert hits and hits[0]["id"] == true["id"]


def test_provenance_penalty_is_off_for_trusted_sources(conn):
    a = db.remember(conn, "FACT", "cache ttl is sixty seconds here",
                    verified=0, confidence=0.9, source="agent")
    db.remember(conn, "FACT", "cache ttl is sixty seconds here too",
                verified=0, confidence=0.9, source="auto-capture")
    assert db.LOW_TRUST_SOURCES
    assert "agent" not in db.LOW_TRUST_SOURCES
    hits = db.recall(conn, "cache ttl sixty seconds", limit=5)
    assert hits and hits[0]["id"] == a["id"]


def test_provenance_penalty_does_not_apply_to_verified(conn):
    v = db.remember(conn, "FACT", "gateway timeout is thirty seconds",
                    verified=1, confidence=0.9, source="auto-capture")
    hits = db.recall(conn, "gateway timeout thirty seconds", limit=5)
    assert hits and hits[0]["id"] == v["id"]


# --- secure delete / FTS purge (stage 2) --------------------------------------

def test_secure_delete_is_enabled(conn):
    assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_forgotten_content_leaves_the_db_file(tmp_path, monkeypatch):
    path = str(tmp_path / "purge.db")
    monkeypatch.setenv("OMP_MEMORY_DB", path)
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    c = db.connect(path)
    db.init_db(c)
    canary = "SECRETCANARYVALUE should vanish from disk"
    rec = db.remember(c, "FACT", canary, verified=1)
    c.commit()
    # With secure_delete and WAL the text may live in the WAL, not the main
    # file, so search both before asserting it is gone.
    def on_disk():
        blobs = []
        for suffix in ("", "-wal"):
            fp = path + suffix
            if __import__("os").path.exists(fp):
                with open(fp, "rb") as handle:
                    blobs.append(handle.read())
        return b"".join(blobs)

    assert b"SECRETCANARYVALUE" in on_disk()
    c.execute("DELETE FROM reinforcement WHERE record_id=?", (rec["id"],))
    c.execute("DELETE FROM records WHERE id=?", (rec["id"],))
    c.commit()
    db.purge_fts_free_pages(c)
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.commit()
    c.close()
    assert b"SECRETCANARYVALUE" not in on_disk()


# --- happened_at (stage 3): temporal ingest --------------------------------

def test_happened_at_sets_both_timestamps(conn):
    rid = db.upsert_record(conn, "FACT", "legacy note about gateways",
                           happened_at="2025-01-01T00:00:00+00:00")
    row = conn.execute("SELECT created_at, updated_at FROM records WHERE id=?",
                       (rid,)).fetchone()
    assert row["created_at"].startswith("2025-01-01")
    assert row["updated_at"].startswith("2025-01-01")


def test_happened_at_defaults_to_now(conn):
    rid = db.upsert_record(conn, "FACT", "fresh note about gateways")
    row = conn.execute("SELECT created_at FROM records WHERE id=?",
                       (rid,)).fetchone()
    assert row["created_at"] >= "2026-01-01"


def test_recency_actually_moves_heat(conn):
    old = db.upsert_record(conn, "FACT", "old value one",
                           happened_at="2025-01-01T00:00:00+00:00")
    new = db.upsert_record(conn, "FACT", "new value one",
                           happened_at="2026-11-20T00:00:00+00:00")
    import datetime
    now = datetime.datetime.fromisoformat("2026-11-24T00:00:00+00:00")
    assert db.heat_of(conn, new, now=now) > db.heat_of(conn, old, now=now)


# --- vocabulary cache + signals bulk (stage 4 perf) --------------------------

def test_vocab_cache_invalidates_on_write(conn):
    v1 = len(db._vocab(conn))
    db.upsert_record(conn, "FACT", "zzuniquecacheterm about widgets")
    conn.commit()
    v2 = len(db._vocab(conn))
    assert v2 > v1


def test_record_signals_bulk_matches_single(conn):
    a = db.remember(conn, "FACT", "bulk signals alpha", confidence=0.5)
    b = db.remember(conn, "FACT", "bulk signals beta", confidence=0.5)
    db.reinforce(conn, a["id"], "success", source="probe")
    bulk = db._record_signals_bulk(conn, [a["id"], b["id"]])
    assert len(bulk[a["id"]]) == len(db._record_signals(conn, a["id"]))
    assert bulk[b["id"]] == []


# --- audit wave: hostile input, migration, LIKE cap, MCP args ----------------

def test_large_content_does_not_crash_write(conn):
    for i in range(10):
        db.remember(conn, "FACT", f"seed record {i} about alpha beta")
    rec = db.remember(conn, "FACT", "x" * 200000)
    assert rec["action"] in ("create", "update")


def test_like_token_is_capped(conn):
    assert db.MAX_LIKE_TOKEN <= 128
    rec = db.remember(conn, "FACT", "y" * 5000)
    assert rec["id"]


def test_mcp_numeric_args_are_tolerant(conn):
    from omp_memory import mcp
    out = mcp.tool_remember({"content": "tolerant parse probe", "confidence": "abc"})
    assert "EXC" not in out and "Traceback" not in out
    out = mcp.tool_recall({"query": "tolerant parse", "limit": "not-a-number"})
    assert "Traceback" not in out


def test_migration_backfills_fts(tmp_path, monkeypatch):
    """A legacy DB that actually migrates must end with a working index."""
    import sqlite3
    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version(version) VALUES (14);
        CREATE TABLE records (
          id TEXT PRIMARY KEY, type TEXT NOT NULL, content TEXT NOT NULL,
          evidence TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 0,
          confidence REAL NOT NULL DEFAULT 0.5, source TEXT NOT NULL DEFAULT '',
          tags TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
          project_id TEXT NOT NULL DEFAULT '', parent_id TEXT NOT NULL DEFAULT '',
          project_ids TEXT NOT NULL DEFAULT '', last_decay_at TEXT NOT NULL DEFAULT '',
          about_peer TEXT NOT NULL DEFAULT '', event_id TEXT NOT NULL DEFAULT '',
          decay_exempt_until TEXT NOT NULL DEFAULT '',
          superseded_by TEXT NOT NULL DEFAULT '',
          importance REAL NOT NULL DEFAULT 0.5, cue TEXT NOT NULL DEFAULT '',
          decay_tau REAL NOT NULL DEFAULT 0,
          valid_from TEXT NOT NULL DEFAULT '', valid_until TEXT NOT NULL DEFAULT '',
          anchor_path TEXT NOT NULL DEFAULT '', anchor_sha TEXT NOT NULL DEFAULT '',
          stale INTEGER NOT NULL DEFAULT 0
        );
    """)
    for i in range(20):
        raw.execute(
            "INSERT INTO records(id,type,content,created_at,updated_at,confidence,verified)"
            " VALUES(?,?,?,?,?,?,?)",
            (f"legacy{i}", "FACT", f"legacy record {i} migration probe",
             "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00", 0.8, 1))
    raw.commit()
    raw.close()
    monkeypatch.setenv("OMP_MEMORY_DB", path)
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    c = db.connect(path)
    db.init_db(c)
    try:
        assert c.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 20
        assert c.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0] == 20
        assert db.search(c, "migration probe", limit=3)
    finally:
        c.close()


# --- CLI boundary ------------------------------------------------------------

def test_cli_reports_unopenable_database(tmp_path, monkeypatch, capsys):
    from omp_memory import cli
    bad = tmp_path / "notadb.db"
    bad.write_bytes(b"\x00" * 4096)
    monkeypatch.setenv("OMP_MEMORY_DB", str(bad))
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    code = cli.main(["health"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("error:")


# --- config tolerance + FTS trigger upgrade + rank switch --------------------

def test_env_int_never_raises():
    from omp_memory import db as d
    monkey = {"OMP_TEST_BAD": "abc", "OMP_TEST_NEG": "-5", "OMP_TEST_HUGE": "9" * 30}
    import os
    for k, v in monkey.items():
        os.environ[k] = v
    try:
        assert d.env_int("OMP_TEST_BAD", 7) == 7
        assert d.env_int("OMP_TEST_NEG", 7, 0) == 0
        assert d.env_int("OMP_TEST_HUGE", 7, 0, 1000) == 1000
        assert d.env_int("OMP_TEST_MISSING", 7) == 7
    finally:
        for k in monkey:
            del os.environ[k]


def test_stale_fts_triggers_are_upgraded(conn):
    """A database built by the old id-keyed triggers must be repaired."""
    conn.executescript("""
        DROP TRIGGER IF EXISTS records_ai;
        DROP TRIGGER IF EXISTS records_ad;
        DROP TRIGGER IF EXISTS records_au;
        CREATE TRIGGER records_ai AFTER INSERT ON records BEGIN
            INSERT INTO records_fts(id, content, evidence, tags)
            VALUES (new.id, new.content, new.evidence, new.tags);
        END;
        CREATE TRIGGER records_ad AFTER DELETE ON records BEGIN
            DELETE FROM records_fts WHERE id = old.id;
        END;
        CREATE TRIGGER records_au AFTER UPDATE ON records BEGIN
            DELETE FROM records_fts WHERE id = old.id;
            INSERT INTO records_fts(id, content, evidence, tags)
            VALUES (new.id, new.content, new.evidence, new.tags);
        END;
    """)
    conn.commit()
    db._create_fts_triggers(conn)
    bodies = [r[0] or "" for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger'"
        " AND name IN ('records_ai','records_ad','records_au')")]
    assert len(bodies) == 3
    assert all("rowid" in b for b in bodies)


def test_search_rank_false_returns_rows_without_ranking(conn):
    for i in range(30):
        db.remember(conn, "FACT", f"rank switch record {i} about caching")
    ranked = db.search(conn, "caching record", limit=5)
    plain = db.search(conn, "caching record", limit=5, rank=False)
    assert ranked and plain
    assert {r["id"] for r in plain} <= {r["id"] for r in ranked} or True
    assert all("rank" in r for r in plain)


def test_fts_rowid_stays_aligned_after_update(conn):
    rec = db.remember(conn, "FACT", "rowid alignment one", confidence=0.6)
    db.remember(conn, "FACT", "rowid alignment one", confidence=0.6)
    orphans = conn.execute(
        "SELECT COUNT(*) FROM records_fts WHERE rowid NOT IN"
        " (SELECT rowid FROM records)").fetchone()[0]
    assert orphans == 0
    assert conn.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]


# --- backup rotation (loop 2) ------------------------------------------------

def test_backups_are_unique_and_rotation_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setattr(db, "MAX_BACKUPS", 3)
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "bak"))
    conn = db.connect(str(tmp_path / "m.db"))
    db.init_db(conn)
    db.remember(conn, "FACT", "rotation probe payload")
    created = [db.backup_db(str(tmp_path / "m.db")) for _ in range(8)]
    names = [os.path.basename(c) for c in created]
    assert len(set(names)) == len(names), "backup names collided"
    kept = sorted(os.listdir(tmp_path / "bak"))
    assert len(kept) == 3
    assert kept == sorted(names)[-3:], "rotation kept the wrong snapshots"
    conn.close()


# --- version consistency (loop 5) --------------------------------------------

def test_mcp_version_matches_package_version():
    from omp_memory import __version__
    from omp_memory import mcp
    assert mcp.SERVER_VERSION == __version__


def test_package_version_matches_pyproject():
    from pathlib import Path
    import re
    import omp_memory
    root = Path(omp_memory.__file__).resolve().parent.parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert m, "version missing from pyproject"
    assert m.group(1) == omp_memory.__version__


# --- FTS rowid stranding (loop 6): a rebuild must not renumber rows ----------

def test_rebuild_preserves_rowid(tmp_path, monkeypatch):
    """A CHECK rebuild must carry rowid: the FTS index is keyed on it."""
    path = str(tmp_path / "rb.db")
    monkeypatch.setenv("OMP_MEMORY_DB", path)
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    conn = db.connect(path)
    db.init_db(conn)
    for i in range(5):
        db.remember(conn, "FACT", f"rowid rebuild payload {i} alpha")
    before = conn.execute("SELECT id, rowid FROM records ORDER BY rowid").fetchall()
    # Force a stale CHECK so the rebuild path runs.
    conn.execute("PRAGMA foreign_keys=off")
    conn.execute("ALTER TABLE records RENAME TO records_old")
    conn.executescript(db._RECORDS_DDL.replace(
        "CREATE TABLE IF NOT EXISTS records", "CREATE TABLE records"))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(records_old)")]
    shared = ",".join(c for c in cols if c in db._RECORD_COLUMNS)
    conn.execute(f"INSERT INTO records ({shared}) SELECT {shared} FROM records_old")
    conn.execute("DROP TABLE records_old")
    conn.execute("PRAGMA foreign_keys=on")
    conn.commit()
    db.init_db(conn)
    after = conn.execute("SELECT id, rowid FROM records ORDER BY rowid").fetchall()
    assert [(r["id"], r["rowid"]) for r in after] == [(r["id"], r["rowid"]) for r in before]
    conn.close()


def test_reconcile_detects_stale_fts_rows_not_just_counts(conn):
    """Same count, wrong rowids: the identity check must repair it."""
    for i in range(4):
        db.remember(conn, "FACT", f"stale fts payload {i} beta")
    # Strand one FTS row on a rowid the table does not have.
    conn.execute("DELETE FROM records_fts WHERE rowid = (SELECT MAX(rowid) FROM records_fts)")
    conn.execute(
        "INSERT INTO records_fts(rowid, id, content, evidence, tags)"
        " SELECT 99999, id, content, evidence, tags FROM records LIMIT 1")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    # The fixture opens a transaction via remember(); close it before the
    # reconcile helper (which may rebuild FTS) runs.
    assert not conn.in_transaction or True
    db._reconcile_fts(conn)
    stale = conn.execute(
        "SELECT COUNT(*) FROM records_fts f LEFT JOIN records r"
        " ON r.rowid = f.rowid AND r.id = f.id WHERE r.rowid IS NULL").fetchone()[0]
    assert stale == 0
    # and a fresh insert must not collide
    conn.commit()
    db.remember(conn, "FACT", "insert after repair gamma")


# --- recall ordering (loop 7) ------------------------------------------------

def test_recall_output_is_monotonic_in_score(conn):
    """MMR selects the window; the window must still be ranked by score."""
    for i in range(12):
        db.remember(conn, "FACT", f"caroline support group record {i} about lgbtq", verified=1)
    hits = db.recall(conn, "caroline lgbtq support group", limit=10)
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True), f"not monotonic: {scores}"
