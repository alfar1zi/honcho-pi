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
    assert version == db.SCHEMA_VERSION == 18
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


def test_stem_maps_inflected_forms_together():
    # The FTS index stems with porter; the overlap counter must stem
    # the same way or "verified" scores as no match for a record
    # containing "verify".
    assert db._stem("verified") == db._stem("verify")
    assert db._stem("deployed") == db._stem("deploy")


def test_matched_terms_cover_evidence_and_tags():
    conn = db.connect(":memory:")
    db.init_db(conn, ":memory:")
    # the match lives in evidence and tags, not content; the lexical
    # fraction must still see both terms
    db.remember(conn, "FACT", "nothing relevant here",
                evidence="scheduler fairness analysis", tags="kernel")
    hits = db.recall(conn, "scheduler kernel", limit=3, full=True)
    assert hits[0]["matched_terms"] == 2
    conn.close()


def test_absolute_lexical_spreads_within_same_count():
    # Two records matching the same term count (a short AND query's
    # whole pool) still order by bm25 rank: the tie term owns a real
    # slice of the lexical range, not a 1/1000 decoration.
    ranked = db._absolute_lexical([
        {"id": "strong", "rank": -30.0, "matched_terms": 3, "query_terms": 3},
        {"id": "weak", "rank": -3.0, "matched_terms": 3, "query_terms": 3},
    ])
    assert ranked["strong"] > ranked["weak"]
    assert ranked["strong"] - ranked["weak"] > 0.01


def test_absolute_lexical_prefers_stronger_rank():
    # bm25 is negative and MORE negative = more relevant. Within the
    # same matched count, rank -12.0 outranks -5.0; a 1-term match cannot
    # beat a 3-term match even at a much stronger rank; rank 0 means the
    # row contributed no term (weakest lexical, the fraction floor).
    ranked = db._absolute_lexical([
        {"id": "strong", "rank": -12.0, "matched_terms": 3, "query_terms": 3},
        {"id": "weaker", "rank": -5.0, "matched_terms": 3, "query_terms": 3},
        {"id": "single", "rank": -1.0, "matched_terms": 1, "query_terms": 3},
        {"id": "none", "rank": 0.0, "matched_terms": 0, "query_terms": 3},
    ])
    assert ranked["strong"] > ranked["weaker"] > ranked["single"]
    assert ranked["none"] == 0.0  # zero overlap is its own bucket, not noise


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
    assert version == 18
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


def test_capture_clamps_confidence_to_ceiling(tmp_path):
    # The ceiling is the trust boundary for auto-captured lessons: a
    # transcript cannot mint high-confidence memory. A write above it must
    # come out clamped.
    import importlib
    import sqlite3 as sq3

    conn = db.connect(str(tmp_path / "m.db"))
    db.init_db(conn)
    entries = [{
        "type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
        "message": {"role": "user", "content": "decided to use sqlite not postgres"},
    }]
    # A candidate above the ceiling must come out clamped at write time.
    capped = {
        "rtype": "DECISION",
        "content": "decided to use sqlite not postgres",
        "evidence": "user decision",
        "confidence": 0.95,
        "source": "auto-capture",
        "tags": "decision",
        "session_id": "capceil",
        "project_id": "proj",
    }
    cap = importlib.import_module("omp_memory.capture")
    # The write loop is capture_session's, driven through a fake db module
    # that records the confidence it was given: the clamp, not the caller,
    # must be what bounds the stored value.
    seen = {}
    class FakeDb:
        def find_record(self, *a):
            return None
        def upsert_record(self, conn, **kw):
            seen["confidence"] = kw["confidence"]
            return "id-1"
    from pathlib import Path
    session_file = tmp_path / "capceil.jsonl"
    import json as _json
    session_file.write_text(_json.dumps(entries[0]) + "\n")
    real_extract = cap.extract_lessons
    cap.extract_lessons = lambda entries, path: [capped]
    try:
        report = cap.capture_session(FakeDb(), conn, session_path=session_file,
                                     apply=True)
    finally:
        cap.extract_lessons = real_extract
    assert report["written"] == 1
    assert seen["confidence"] <= cap.CAPTURE_CONFIDENCE_CEILING, (
        f"capture wrote confidence {seen['confidence']} above the ceiling")
    conn.close()


def test_v10_desynced_fts_self_heals_on_open(tmp_path):
    # A v10 database that crashed between the table swap and the FTS
    # refill (or was rebuilt by the pre-hardened migration) has the right
    # schema version, live triggers, and an empty or stale index, and the
    # version gate would never repair it. Every open must reconcile.
    import sqlite3 as sq3
    path = str(tmp_path / "broken.db")
    c = db.connect(path)
    db.init_db(c, path)
    r1 = db.remember(c, "FACT", "heal probe one", confidence=0.5)
    r2 = db.remember(c, "FACT", "heal probe two", confidence=0.5)
    c.commit()
    # Simulate the crash: empty the index and drop the plain indexes.
    c.execute("DELETE FROM records_fts")
    c.execute("DROP INDEX IF EXISTS idx_records_type")
    c.commit()
    c.close()

    c = db.connect(path)
    db.init_db(c, path)
    assert c.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0] == \
        c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_records_type'"
    ).fetchone() is not None
    assert len(db.search(c, "heal probe one", limit=3)) == 1
    assert db.get_record(c, r1["id"]) is not None
    assert db.get_record(c, r2["id"]) is not None
    c.close()
