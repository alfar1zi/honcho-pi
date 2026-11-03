"""Wave 4 tests: auto-handoff detects model switches and builds an evidence brief."""
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

HANDOFF_DIR = Path("/home/farizi/.omp/profiles/max/agent/honcho")
pytestmark = pytest.mark.skipif(
    not (HANDOFF_DIR / "handoff.py").exists(),
    reason="local handoff adapter not installed on this machine",
)


@pytest.fixture()
def handoff(tmp_path, monkeypatch):
    """Import handoff.py with sessions and memory pointed at fixtures."""
    sys.path.insert(0, str(HANDOFF_DIR))
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "memory.db"))
    sessions = tmp_path / "sessions" / "proj"
    sessions.mkdir(parents=True)
    monkeypatch.setattr("handoff.sessions_dir", lambda: sessions.parent)
    try:
        import handoff
        yield handoff, sessions
    finally:
        sys.path.pop(0)


def _write_session(sessions: Path, name: str, entries: list[dict]) -> Path:
    p = sessions / name
    p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return p


def _entries(models: list[str]) -> list[dict]:
    out = [{"type": "session", "timestamp": "2026-11-18T08:00:00.000Z"}]
    for m in models:
        out.append({"type": "model_change", "model": m,
                    "timestamp": "2026-11-18T08:00:01.000Z"})
    return out


def test_model_state_collapses_duplicate_switches(handoff):
    h, _ = handoff
    st = h.model_state(_entries(["m/a", "m/a", "m/b", "m/b", "m/b", "m/c"]))
    assert st.current == "m/c"
    assert st.previous == "m/b"
    assert st.changes == 2
    assert st.switched


def test_model_state_no_switch_when_single_model(handoff):
    h, _ = handoff
    st = h.model_state(_entries(["m/a", "m/a"]))
    assert not st.switched
    assert st.current == "m/a"


def test_detect_reports_latest_session(handoff):
    h, sessions = handoff
    _write_session(sessions, "a.jsonl", _entries(["m/a"]))
    b = _write_session(sessions, "b.jsonl", _entries(["m/a", "m/b"]))
    b.touch()
    out = h.detect()
    assert out["session"].endswith("b.jsonl")
    assert out["current"] == "m/b"
    assert out["switched"]


def test_build_brief_carries_user_turns_failures_and_files(handoff):
    h, sessions = handoff
    _write_session(sessions, "a.jsonl", [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "user", "content": "fix the recall ranking"}},
        {"type": "message", "timestamp": "2026-11-18T08:00:05.000Z",
         "message": {"role": "toolResult", "toolName": "bash",
                     "content": "pytest exited with code 1"}},
        {"type": "message", "timestamp": "2026-11-18T08:00:06.000Z",
         "message": {"role": "toolResult", "toolName": "read",
                     "content": "ok"}},
        {"type": "custom", "input": {"path": "/repo/src/db.py"}},
    ] + _entries(["m/a", "m/b"]))
    brief = h.build()
    assert brief["model"]["switched"]
    text = brief["text"]
    assert "fix the recall ranking" in text
    assert "exited with code 1" in text
    assert "/repo/src/db.py" in text
    assert "m/a -> m/b" in text


def test_build_skips_control_text_and_memory_fence(handoff):
    h, sessions = handoff
    _write_session(sessions, "a.jsonl", [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "user", "content":
                     "[System note] recalled memory context: secret should not appear"}},
        {"type": "message", "timestamp": "2026-11-18T08:00:01.000Z",
         "message": {"role": "user", "content": "real request"}},
    ] + _entries(["m/a"]))
    text = h.build()["text"]
    assert "secret should not appear" not in text
    assert "real request" in text


def test_memories_section_reads_database(handoff, tmp_path):
    h, sessions = handoff
    from omp_memory import db
    conn = db.connect(str(tmp_path / "memory.db"))
    db.init_db(conn, str(tmp_path / "memory.db"))
    db.remember(conn, rtype="DECISION", content="use porter tokenizer",
                source="session", confidence=0.9)
    conn.commit()
    conn.close()
    _write_session(sessions, "a.jsonl", _entries(["m/a", "m/b"]))
    text = h.build()["text"]
    assert "use porter tokenizer" in text
    assert "DECISION" in text


def test_truncation_respects_budget(handoff):
    h, sessions = handoff
    # _clip caps each section, so the budget must be crossed by section count.
    entries = [{"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
                "message": {"role": "user", "content": f"request {i} " + "x" * 600}}
               for i in range(12)]
    entries += _entries(["m/a"])
    _write_session(sessions, "a.jsonl", entries)
    brief = h.build(max_chars=1000)
    assert brief["chars"] <= 1000
    assert brief["text"].endswith("[handoff truncated to fit context budget]")
