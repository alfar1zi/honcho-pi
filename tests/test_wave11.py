"""Wave F: event-boundary capture.

A session log is not one memory episode. _segments splits it at time gaps
and topic shifts; extract_lessons emits candidates per event, each carrying
an event_id and an event tag. Idempotency and secret scanning survive.
"""
import json

import pytest

from omp_memory import capture, db


def _msg(role, content, ts=None):
    e = {"type": "message", "message": {"role": role, "content": content}}
    if ts is not None:
        e["timestamp"] = ts
    return e


def _two_event_entries():
    return [
        _msg("user", "deploy to prod failed with docker buildx error",
             "2026-11-18T08:00:00.000Z"),
        _msg("toolResult", "pytest exited with code 1",
             "2026-11-18T08:01:00.000Z"),
        _msg("user", "refactor the grafana dashboard to use recharts",
             "2026-11-18T08:02:00.000Z"),
        _msg("user", "decided to use recharts for the dashboard",
             "2026-11-18T08:03:00.000Z"),
    ]


def test_segments_split_on_topic_shift():
    segs = capture._segments(_two_event_entries())
    assert [s["index"] for s in segs] == [0, 1]
    # every entry lands in exactly one segment
    assert sum(len(s["entries"]) for s in segs) == 4


def test_segments_keep_same_topic_together():
    entries = [
        _msg("user", "docker buildx deploy failing", "2026-11-18T08:00:00.000Z"),
        _msg("user", "docker buildx deploy still failing", "2026-11-18T08:01:00.000Z"),
    ]
    segs = capture._segments(entries)
    assert [s["index"] for s in segs] == [0]


def test_segments_split_on_time_gap():
    entries = [
        _msg("user", "docker buildx deploy failing", "2026-11-18T08:00:00.000Z"),
        _msg("user", "docker buildx deploy failing again", "2026-11-18T12:00:00.000Z"),
    ]
    segs = capture._segments(entries)
    assert [s["index"] for s in segs] == [0, 1]


def test_segments_split_timestampless_on_topic_shift():
    # no timestamps: no time boundary, but topic shifts still split.
    # Three alternating turns -> three events, one per shift.
    entries = [
        _msg("user", "docker buildx deploy failing"),
        _msg("user", "refactor dashboard to recharts"),
        _msg("user", "docker buildx deploy failing again"),
    ]
    segs = capture._segments(entries)
    assert [s["index"] for s in segs] == [0, 1, 2]


def test_segments_keep_timestampless_same_topic_together():
    # no timestamps, no topic shift: one event even though entries carry
    # no ts (they follow the current segment, never split on missing ts).
    entries = [
        _msg("user", "docker buildx deploy failing"),
        _msg("toolResult", "ok"),
        _msg("user", "docker buildx deploy still failing"),
    ]
    segs = capture._segments(entries)
    assert [s["index"] for s in segs] == [0]


def test_extract_lessons_emits_event_ids(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in _two_event_entries()))
    cands = capture.extract_lessons(capture._read_entries(p), p)
    assert cands, "expected at least one candidate"
    by_event = {}
    for c in cands:
        assert c["event_id"].startswith("a#e")
        assert any(t.startswith("event:") for t in c["tags"].split(","))
        by_event.setdefault(c["event_id"], []).append(c)
    assert len(by_event) >= 2, "two distinct events should yield distinct ids"


def test_extract_lessons_same_topic_stays_one_event(tmp_path):
    entries = [
        _msg("user", "docker buildx deploy failing", "2026-11-18T08:00:00.000Z"),
        _msg("user", "decided to use buildx for the deploy",
             "2026-11-18T08:01:00.000Z"),
    ]
    p = tmp_path / "a.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))
    cands = capture.extract_lessons(capture._read_entries(p), p)
    assert {c["event_id"] for c in cands} == {"a#e0"}


@pytest.fixture()
def session_dir(tmp_path, monkeypatch):
    """Point capture at a fake sessions root (same shape as test_wave5)."""
    root = tmp_path / "sessions" / "proj"
    root.mkdir(parents=True)
    monkeypatch.setenv("OMP_MEMORY_SESSIONS", str(root.parent))
    return root


def _write_jsonl(p, entries):
    p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return p


def test_recapture_is_idempotent_across_events(conn, session_dir):
    p = _write_jsonl(session_dir / "a.jsonl", _two_event_entries())
    first = capture.capture_session(db, conn, apply=True)
    assert first["written"] > 0
    second = capture.capture_session(db, conn, apply=True)
    assert second["written"] == 0
    # event_id must not enter record identity: re-capture of the same
    # content maps to the same record, so the second pass writes nothing
    # even though the event ids are recomputed identically.


def test_secret_scan_drops_candidates_in_any_event(tmp_path):
    entries = [
        _msg("user", "deploy to prod failed with docker buildx error",
             "2026-11-18T08:00:00.000Z"),
        _msg("toolResult", "Traceback... auth failed: sk-abcdefghijklmnop1234",
             "2026-11-18T08:01:00.000Z"),
        _msg("user", "refactor the grafana dashboard to use recharts",
             "2026-11-18T08:02:00.000Z"),
    ]
    p = tmp_path / "a.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))
    cands = capture.extract_lessons(capture._read_entries(p), p)
    # the secret-bearing toolResult is the only failure candidate and the
    # second event carries no decision keyword, so nothing survives: the
    # scan drops per candidate, not per event.
    assert cands == []


def test_capture_session_still_compatible_with_monkeypatched_extract(tmp_path):
    """test_wave3 clamps confidence via extract_lessons monkeypatch; the
    signature and call shape must stay (entries, session_path) positional."""
    conn = db.connect(str(tmp_path / "m.db"))
    db.init_db(conn)
    capped = {
        "rtype": "INFERENCE", "content": "x", "evidence": "e",
        "confidence": 0.9, "source": "auto-capture", "session_id": "a",
        "project_id": "proj", "tags": "t", "event_id": "a#e0",
    }
    seen = {}

    class FakeDb:
        def find_record(self, *a):
            return None

        def upsert_record(self, c, **kw):
            seen.update(kw)
            return "id-1"

    session_file = tmp_path / "capceil.jsonl"
    session_file.write_text(json.dumps(_msg("user", "hi")))
    real = capture.extract_lessons
    capture.extract_lessons = lambda entries, path: [capped]
    try:
        report = capture.capture_session(FakeDb(), conn, session_path=session_file,
                                         apply=True)
    finally:
        capture.extract_lessons = real
    assert report["written"] == 1
    assert seen["event_id"] == "a#e0"
    conn.close()


# --- Wave G: PROCEDURAL / REFLECTIVE types -----------------------------------

def test_procedural_reflective_are_valid_types():
    from omp_memory import db as d
    assert "PROCEDURAL" in d.VALID_TYPES
    assert "REFLECTIVE" in d.VALID_TYPES
    assert set(d.TYPE_PRIOR.keys()) == d.VALID_TYPES
    assert set(d.TYPE_BM25_WEIGHTS.keys()) == d.VALID_TYPES


def test_procedural_reflective_roundtrip(tmp_path):
    from omp_memory import db as d
    conn = d.connect(str(tmp_path / "m.db"))
    d.init_db(conn)
    assert d.SCHEMA_VERSION == 14
    r1 = d.remember(conn, "PROCEDURAL",
                    "migrate with rebuild_records_check swap then reconcile fts",
                    verified=1)
    r2 = d.remember(conn, "REFLECTIVE",
                    "jaccard boundary needs the turn tokens kept across events",
                    verified=1)
    hits = d.recall(conn, "rebuild records check swap", limit=3)
    assert hits and hits[0]["id"] == r1["id"]
    hits2 = d.recall(conn, "jaccard boundary turn tokens", limit=3)
    assert hits2 and hits2[0]["id"] == r2["id"]
    # the CHECK accepts the new types; a bad type still raises
    import pytest
    with pytest.raises(ValueError):
        d.remember(conn, "NOT_A_TYPE", "x")
    conn.close()


def test_migration_v13_to_v14_rebuilds_check(tmp_path):
    """A database already at v13 (or v12) replays the rebuild on upgrade."""
    from omp_memory import db as d
    conn = d.connect(str(tmp_path / "m.db"))
    d.init_db(conn)
    # force the live CHECK back to the v13 spelling, keep version 13
    conn.execute("DROP TABLE records")
    conn.execute("""CREATE TABLE records (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL CHECK (type IN ('FACT','ASSUMPTION','INFERENCE',
            'DECISION','RULE','CORRECTION','INTENT','WORKFLOW')),
        content TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
        verified INTEGER NOT NULL DEFAULT 0, confidence REAL NOT NULL DEFAULT 0.5,
        source TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '', project_id TEXT NOT NULL DEFAULT '',
        parent_id TEXT NOT NULL DEFAULT '', project_ids TEXT NOT NULL DEFAULT '',
        last_decay_at TEXT NOT NULL DEFAULT '', about_peer TEXT NOT NULL DEFAULT '',
        event_id TEXT NOT NULL DEFAULT '',
        decay_exempt_until TEXT NOT NULL DEFAULT '',
        superseded_by TEXT NOT NULL DEFAULT '',
        importance REAL NOT NULL DEFAULT 0.5, cue TEXT NOT NULL DEFAULT '')""")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (13)")
    conn.commit()
    d.init_db(conn)  # replays v14: rebuild + reconcile
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 14
    rid = d.remember(conn, "PROCEDURAL", "procedure after rebuild", verified=1)
    assert rid
    conn.close()
