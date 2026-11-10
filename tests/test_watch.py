import json
import time

import pytest

from omp_memory import db
from omp_memory import watch


@pytest.fixture
def wenv(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "STATE_FILE", tmp_path / "watch_state.json")
    return tmp_path


def test_should_capture_when_idle_and_new(wenv):
    now = time.time()
    assert watch.should_capture(mtime=now - 700, now=now, captured_mtime=None) is True


def test_not_capture_when_recent(wenv):
    now = time.time()
    assert watch.should_capture(mtime=now - 60, now=now, captured_mtime=None) is False


def test_not_capture_when_already_captured(wenv):
    now = time.time()
    mtime = now - 700
    assert watch.should_capture(mtime=mtime, now=now, captured_mtime=mtime) is False
    assert watch.should_capture(mtime=mtime, now=now, captured_mtime=mtime + 1) is False


def test_capture_once_writes_and_records_state(wenv, monkeypatch, tmp_path):
    db_file = tmp_path / "memory.db"
    monkeypatch.setenv("OMP_MEMORY_DB", str(db_file))
    monkeypatch.setattr(watch, "IDLE_SECONDS", 0)
    session = tmp_path / "sessions" / "-Documents-project-honcho-pi" / "2026-01-01T00-00-00Z_x.jsonl"
    session.parent.mkdir(parents=True)
    entry = {
        "type": "message",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "honcho-pi uses wal mode for sqlite, verified"}],
        },
    }
    session.write_text(json.dumps(entry), encoding="utf-8")
    import os
    past = time.time() - 1000
    os.utime(session, (past, past))
    monkeypatch.setattr(watch, "latest_session", lambda: session)
    monkeypatch.setattr(watch, "sessions_dir", lambda: session.parent.parent)

    conn = db.connect(str(db_file))
    db.init_db(conn)
    result = watch.capture_once(db, conn, now=time.time())
    conn.close()
    assert result["action"] == "captured"
    assert result["project"] == "honcho-pi"
    state = watch.load_state()
    assert state["captured_mtime"] == past


def test_capture_once_skips_when_not_idle(wenv, monkeypatch, tmp_path):
    db_file = tmp_path / "memory.db"
    monkeypatch.setenv("OMP_MEMORY_DB", str(db_file))
    session = tmp_path / "sessions" / "-tmp" / "s.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watch, "latest_session", lambda: session)
    monkeypatch.setattr(watch, "sessions_dir", lambda: session.parent.parent)

    conn = db.connect(str(db_file))
    db.init_db(conn)
    result = watch.capture_once(db, conn, now=time.time())
    conn.close()
    assert result["action"] == "none"


def test_session_project_name(wenv, monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    for folder, expected in [
        ("-Documents-project-honcho-pi", "honcho-pi"),
        ("-tmp", "tmp"),
        ("-Documents-backup", "Documents-backup"),
    ]:
        session = root / folder / "s.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(watch, "sessions_dir", lambda r=root: r)
        assert watch._session_project(session) == expected


def test_sleep_interval_runs_sleep_pass(tmp_path, monkeypatch):
    from omp_memory import watch, db as memory_db

    monkeypatch.setattr(watch, "STATE_FILE", tmp_path / "state.json")
    db_path = tmp_path / "w.db"
    conn = memory_db.connect(str(db_path))
    memory_db.init_db(conn)
    a = memory_db.remember(conn, "INFERENCE", "the deploy needs free disk space",
                           confidence=0.6)
    b = memory_db.remember(conn, "INFERENCE", "deploy needs free disk space",
                           confidence=0.6)

    report = memory_db.sleep_pass(conn, apply=True)
    assert report["apply"] is True
    assert report["merged"], "near-duplicates merged by sleep pass"
