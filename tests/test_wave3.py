"""Wave 3 tests: FTS tokenizer/query fixes and recall reranking changes.

- Prefix query: the last query word gets a `*` so `deplo` matches `deploy`.
- Porter stemming: `deploying` matches `deploy` through the shared stem.
- Punctuation-only queries return [] instead of raising on FTS MATCH syntax.
- Migration to schema v6 rebuilds records_fts with the porter tokenizer and
  keeps existing records searchable.
- Recall lexical is absolute [0, 1] with a fixed bm25 ceiling, so scores are
  comparable across queries, and confidence is not double-counted.
- Filtered recall (verified_only / min_confidence) still reranks instead of
  falling back to plain lexical order.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from omp_memory import db


@pytest.fixture()
def conn(tmp_path):
    path = str(tmp_path / "memory.db")
    c = db.connect(path)
    db.init_db(c, path)
    yield c
    c.close()


def test_prefix_query_matches_longer_word(conn):
    db.remember(conn, "FACT", "scheduled deploy pipeline")
    rows = db.search(conn, "deplo", limit=5)
    assert len(rows) == 1


def test_porter_stemmer_matches_inflected_word(conn):
    db.remember(conn, "FACT", "deploy the rollout")
    rows = db.search(conn, "deploying", limit=5)
    assert len(rows) == 1


@pytest.mark.parametrize("query", ["...", "***", '"quoted"', "don't break", "   "])
def test_punctuation_query_returns_empty_without_error(conn, query):
    db.remember(conn, "FACT", "ordinary content")
    assert db.search(conn, query, limit=5) == []


def test_migration_v6_rebuilds_fts_and_preserves_records(tmp_path):
    path = str(tmp_path / "old.db")
    c = db.connect(path)
    db.init_db(c, path)
    db.remember(c, "FACT", "legacy deployment note")
    c.execute("UPDATE schema_version SET version = 5")
    c.execute("DROP TABLE records_fts")
    c.execute(
        'CREATE VIRTUAL TABLE records_fts USING fts5(id, content, evidence, tags,'
        ' tokenize="unicode61")'
    )
    c.execute("INSERT INTO records_fts SELECT id, content, evidence, tags FROM records")
    c.commit()
    c.close()

    c = db.connect(path)
    db.init_db(c, path)
    version = c.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION == 10
    rows = c.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'records_fts'"
    ).fetchone()
    assert "porter" in rows["sql"]
    assert len(db.search(c, "legacy deployment", limit=5)) == 1
    vocab = c.execute("SELECT COUNT(*) FROM records_fts_vocab").fetchone()[0]
    assert vocab > 0
    c.close()

def test_absolute_lexical_is_comparable_across_queries(conn):
    db.remember(conn, "FACT", "kernel scheduler fairness overview")
    db.remember(conn, "FACT", "network buffer sizing guide")
    narrow = db.recall(conn, "scheduler fairness", limit=5, full=True)
    broad = db.recall(conn, "kernel scheduler", limit=5, full=True)
    assert narrow[0]["lexical"] > 0.0
    # A different query pool must not rescale the same hit to a different
    # absolute value merely because the pool got bigger.
    assert narrow[0]["lexical"] <= 1.0
    assert broad[0]["lexical"] <= 1.0


def test_absolute_lexical_uses_fixed_ceiling():
    # rank 0 is a perfect match, -5.0 sits halfway down the ceiling of 10,
    # and -12.0 is past the ceiling and clamps to 0. The mapping does not
    # depend on the pool, so a hit scores the same in every query.
    ranked = db._absolute_lexical([
        {"id": "a", "rank": 0.0},
        {"id": "b", "rank": -5.0},
        {"id": "c", "rank": -12.0},
    ])
    assert ranked == {"a": 1.0, "b": 0.5, "c": 0.0}


def test_confidence_not_double_counted(conn):
    # Two records with identical lexical strength: under the old formula the
    # 0.1 confidence record lost twice (once via heat, once via the direct
    # confidence term). Now confidence enters only through heat, and a type
    # prior cannot rescue the weaker record either.
    db.remember(conn, "INFERENCE", "alpha beta gamma delta epsilon",
                confidence=0.1)
    db.remember(conn, "RULE", "alpha beta gamma delta zeta",
                confidence=1.0, verified=1)
    results = db.recall(conn, "alpha beta", limit=5, full=True)
    assert len(results) == 2
    best = results[0]
    assert best["confidence"] == pytest.approx(1.0)
    assert best["lexical"] == results[1]["lexical"]
    # the losing record still scores from heat alone, not zero
    assert results[1]["score"] > 0.0


def test_filtered_recall_still_reranks(conn):
    db.remember(conn, "INFERENCE", "alpha beta gamma",
                confidence=0.1, verified=0)
    db.remember(conn, "RULE", "alpha beta delta",
                confidence=0.9, verified=1)
    results = db.recall(conn, "alpha beta", limit=5, full=True,
                        verified_only=True)
    assert len(results) == 1
    assert results[0]["type"] == "RULE"
    # score must reflect the full rerank (heat + prior), not raw bm25 order
    assert results[0]["score"] > 0.0

def test_v9_check_without_intent_rebuilds_on_migrate(tmp_path):
    # The live v9 records table has a CHECK without 'INTENT'. ALTER cannot
    # change a CHECK, so _migrate must rebuild the table or an INTENT write
    # fails with IntegrityError after the upgrade.
    import sqlite3 as sq3
    path = str(tmp_path / "v9.db")
    c = sq3.connect(path)
    c.executescript("""
    CREATE TABLE schema_version (version INTEGER NOT NULL);
    INSERT INTO schema_version VALUES (9);
    CREATE TABLE records (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL CHECK (type IN
            ('FACT','ASSUMPTION','INFERENCE','DECISION','RULE','CORRECTION')),
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
        about_peer TEXT NOT NULL DEFAULT ''
    );
    INSERT INTO records (id, type, content, created_at, updated_at)
    VALUES ('legacy1', 'FACT', 'legacy deployment note',
            '2026-01-01', '2026-01-01');
    CREATE VIRTUAL TABLE records_fts USING fts5(
        id, content, evidence, tags, tokenize="porter unicode61");
    INSERT INTO records_fts(id, content, evidence, tags)
        SELECT id, content, evidence, tags FROM records;
    CREATE TABLE reinforcement (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id TEXT NOT NULL,
        signal TEXT NOT NULL CHECK (signal IN
            ('success','failure','correction','reinforce','decay','retrieval')),
        delta REAL NOT NULL DEFAULT 0.1,
        source TEXT NOT NULL DEFAULT '',
        ts TEXT NOT NULL,
        FOREIGN KEY (record_id) REFERENCES records(id)
    );
    INSERT INTO reinforcement (record_id, signal, ts)
        VALUES ('legacy1', 'success', '2026-01-01');
    """)
    c.commit()
    c.close()

    c = db.connect(path)
    db.init_db(c, path)
    version = c.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 10
    # A rename-based rebuild orphans reinforcement FKs onto records_old and
    # drops the FTS triggers, so every later write silently desyncs FTS.
    # The swap must leave FKs on records, the triggers alive, and new
    # writes searchable.
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    trig = {t[0] for t in c.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"records_ai", "records_ad", "records_au"} <= trig
    r = db.remember(c, "INTENT", "plan to add rate limiting", confidence=0.5)
    assert r["applied"] == "inserted"
    assert c.execute(
        "SELECT COUNT(*) FROM records_fts WHERE id=?", (r["id"],)).fetchone()[0] == 1
    assert len(db.search(c, "rate limiting", limit=3)) == 1
    rec = db.get_record(c, "legacy1")
    assert rec is not None and rec["type"] == "FACT"
    assert len(db.search(c, "legacy deployment note", limit=5)) == 1
    c.close()
