"""Wave 4 tests: auto-handoff detects model switches and builds an evidence brief."""
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

# The logic moved into the package (src/omp_memory/handoff.py), so this file no
# longer depends on a machine-local script and never skips in CI.
HANDOFF_DIR = REPO_ROOT / "src"


@pytest.fixture()
def handoff(tmp_path, monkeypatch):
    """The module under test, with sessions and memory pointed at fixtures."""
    from omp_memory import handoff
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "memory.db"))
    sessions = tmp_path / "sessions" / "proj"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(handoff, "sessions_dir", lambda: sessions.parent)
    yield handoff, sessions


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
    # _memories() opens the path from OMP_MEMORY_DB (set by the fixture);
    # this asserts the fixture's DB is the one it reads.
    brief = h.build()
    assert "use porter tokenizer" in brief["text"], brief["text"][:400]
    assert "DECISION" in brief["text"]


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
    # Truncation is now priority-based: whole low-priority sections are
    # dropped and the result says so, instead of slicing the tail mid-line.
    # A budget too small for any body is reported as unusable.
    if brief["usable"]:
        assert "lower-priority section(s) omitted" in brief["text"] \
            or brief["text"].count("```") % 2 == 0
    else:
        assert "skip injection" in brief["text"]
