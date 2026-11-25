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
