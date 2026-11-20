"""Wave 5 tests: session-log capture extracts lessons into memory on demand.

- extract_lessons turns transcript entries into candidate memory records
  with the right type, confidence and source.
- Control text (handoff/system-note injection) is skipped, never captured.
- maintenance op=capture returns candidates without writing by default;
  with apply=true it writes them as low-confidence unverified rows.
- Duplicate runs are a no-op: content identity makes a second capture of
  the same transcript write nothing new.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omp_memory import capture, db

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


@pytest.fixture()
def conn(tmp_path):
    path = str(tmp_path / "memory.db")
    c = db.connect(path)
    db.init_db(c, path)
    yield c
    c.close()


@pytest.fixture()
def session_dir(tmp_path, monkeypatch):
    """Point capture at a fake sessions root."""
    root = tmp_path / "sessions" / "proj"
    root.mkdir(parents=True)
    monkeypatch.setenv("OMP_MEMORY_SESSIONS", str(root.parent))
    return root


def _write_session(root: Path, name: str, entries: list[dict]) -> Path:
    p = root / name
    p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return p


def _session_entries() -> list[dict]:
    return [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "user",
                     "content": "decided to use porter tokenizer for FTS"}},
        {"type": "message", "timestamp": "2026-11-18T08:00:05.000Z",
         "message": {"role": "toolResult", "toolName": "bash",
                     "content": "pytest exited with code 1"}},
        {"type": "message", "timestamp": "2026-11-18T08:00:06.000Z",
         "message": {"role": "toolResult", "toolName": "read",
                     "content": "ok"}},
        {"type": "custom", "input": {"path": "/repo/src/db.py"}},
        {"type": "custom", "input": {"path": "/repo/src/db.py"}},
    ]


def test_extract_lessons_shapes_candidates(session_dir):
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", _session_entries())
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    types = {c["rtype"] for c in cands}
    assert "INFERENCE" in types
    assert "ASSUMPTION" in types
    for c in cands:
        assert c["source"] == "auto-capture"
        assert c["confidence"] <= 0.5
        assert c["session_id"] == "2026-11-18T08-00-00-000Z_a"
        assert c["project_id"] == "proj"


def test_extract_lessons_skips_control_text(session_dir):
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "user", "content":
                     "[System note] recalled memory context: decided to skip this"}},
        {"type": "message", "timestamp": "2026-11-18T08:00:01.000Z",
         "message": {"role": "user", "content": "real request"}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    for c in cands:
        assert "decided to skip this" not in c["content"]
    # The real user turn has no decision keyword, so no decision candidate.
    decision_cands = [c for c in cands if c["tags"].startswith("decision")]
    assert decision_cands == []


def test_capture_dry_run_writes_nothing(conn, session_dir):
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", _session_entries())
    report = capture.capture_session(db, conn, apply=False)
    assert report["written"] == 0
    assert report["apply"] is False
    assert report["candidates"], "dry run must still report candidates"
    assert db.stats(conn)["total"] == 0


def test_capture_apply_writes_records(conn, session_dir):
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", _session_entries())
    report = capture.capture_session(db, conn, apply=True)
    assert report["apply"] is True
    assert report["written"] > 0
    rows = db.list_records(conn, limit=100)
    sources = {r["source"] for r in rows}
    assert sources == {"auto-capture"}
    for r in rows:
        assert r["verified"] == 0
        assert r["confidence"] <= 0.5


def test_capture_is_idempotent(conn, session_dir):
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", _session_entries())
    first = capture.capture_session(db, conn, apply=True)
    second = capture.capture_session(db, conn, apply=True)
    assert first["written"] > 0
    assert second["written"] == 0
    assert db.stats(conn)["total"] == first["written"]


def test_capture_no_session_file(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_MEMORY_SESSIONS", str(tmp_path / "empty"))
    report = capture.capture_session(db, conn)
    assert report.get("error") == "no session file found"


# ---------------------------------------------------------------------------
# MCP end-to-end: maintenance op=capture over stdio
# ---------------------------------------------------------------------------

class Server:
    """Same spawn pattern as test_mcp.py; kept local so this file is standalone."""

    def __init__(self, tmp_path, env_extra=None):
        self.db_path = tmp_path / "memory.db"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(SRC),
            "OMP_MEMORY_DB": str(self.db_path),
            "OMP_MEMORY_BACKUP_DIR": str(tmp_path / "backups"),
        }
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "omp_memory.mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path),
        )
        self.req_id = 0

    def request(self, method, params=None):
        self.req_id += 1
        msg = {"jsonrpc": "2.0", "id": self.req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        assert raw.strip(), "server closed stdout before answering"
        return json.loads(raw)

    def call(self, name, args):
        resp = self.request("tools/call", {"name": name, "arguments": args})
        assert "error" not in resp, resp
        return resp["result"]["content"][0]["text"]

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def test_maintenance_capture_op_end_to_end(tmp_path):
    sessions = tmp_path / "sessions" / "proj"
    sessions.mkdir(parents=True)
    (sessions / "2026-11-18T08-00-00-000Z_a.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _session_entries()), encoding="utf-8")
    srv = Server(tmp_path, env_extra={"OMP_MEMORY_SESSIONS": str(tmp_path / "sessions")})
    try:
        srv.request("initialize", {})
        dry = srv.call("maintenance", {"op": "capture"})
        assert '"written": 0' in dry
        assert '"candidates"' in dry
        wet = srv.call("maintenance", {"op": "capture", "apply": True})
        assert '"written"' in wet
        assert '"written": 0' not in wet
        # Verify the rows landed in the DB the server wrote.
        import sqlite3
        c = sqlite3.connect(str(srv.db_path))
        rows = c.execute("SELECT source, verified FROM records").fetchall()
        c.close()
        assert rows and all(r[0] == "auto-capture" and r[1] == 0 for r in rows)
    finally:
        srv.close()


def test_maintenance_unknown_op_lists_capture(tmp_path):
    srv = Server(tmp_path)
    try:
        srv.request("initialize", {})
        out = srv.call("maintenance", {"op": "bogus"})
        assert "capture" in out
    finally:
        srv.close()


def test_capture_reflect_runs_experience_pass(conn, session_dir):
    """reflect=True abstracts the captured lessons in the same call.

    The captured session's failure and the rule-like decision both land
    in the store; the reflect pass links them by event_id and reports
    the summary under the capture report's 'reflect' key.
    """
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", _session_entries())
    report = capture.capture_session(db, conn, apply=True, reflect=True)
    assert report["written"] > 0
    assert "reflect" in report
    assert set(report["reflect"]) == {"procedural", "reflective", "pairs",
                                      "links", "patterns", "written"}
    assert report["reflect"]["links"] >= 0
    # default (reflect off) keeps the old shape: no 'reflect' key
    plain = capture.capture_session(db, conn, apply=True)
    assert "reflect" not in plain


# --- failure matcher precision (audit: keyword matching captured doc text) ---

def test_read_result_mentioning_error_is_not_a_failure(session_dir):
    """A read of a file whose text contains 'error' is content, not an outcome.

    The old keyword matcher turned every SKILL.md read that mentioned an
    error into a bogus 'Observed failure' INFERENCE; the live store held
    16 such rows, all of them false positives.
    """
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "read",
                     "content": "Traceback (most recent call last): ValueError: x"}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    assert [c for c in cands if "Observed failure" in c["content"]] == []


def test_bash_failure_lines_still_captured(session_dir):
    """Genuine error-shaped failures still produce one failure candidate."""
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "bash",
                     "content": "TypeError: unsupported operand type(s)"}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    fails = [c for c in cands if "Observed failure" in c["content"]]
    assert len(fails) == 1


def test_passing_report_is_not_a_failure(session_dir):
    """'failed: 0' in a passing report must not match; counts must be nonzero."""
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "bash",
                     "content": '{"passed": 10, "failed": 0, "status": "ok"}'}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    assert [c for c in cands if "Observed failure" in c["content"]] == []


def test_bash_echoing_source_is_not_a_failure(session_dir):
    """A bash run that echoes source code mentioning errors is not an outcome."""
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "bash",
                     "content": "bash: except json.JSONDecodeError:"}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    assert [c for c in cands if "Observed failure" in c["content"]] == []


def test_bare_exit_code_is_not_a_lesson(session_dir):
    """'Command exited with code 1' alone is noise, not a lesson."""
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "bash",
                     "content": "Command exited with code 1"}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    assert [c for c in cands if "Observed failure" in c["content"]] == []


def test_parse_error_is_not_a_lesson(session_dir):
    """A tool parse-error fragment is noise, not a lesson."""
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "vibe_list",
                     "content": "Parse Error: Unexpected trailing characters at position 27"}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    assert [c for c in cands if "Observed failure" in c["content"]] == []


def test_lesson_is_the_message_line_not_the_traceback(session_dir):
    """Distillation keeps the exception line, drops traceback boilerplate."""
    body = ("Traceback (most recent call last):\n"
            '  File "<stdin>", line 16, in <module>\n'
            "ValueError: min() iterable argument is empty\n"
            "Command exited with code 1")
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "bash", "content": body}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    fails = [c for c in cands if "Observed failure" in c["content"]]
    assert len(fails) == 1
    assert "ValueError: min() iterable argument is empty" in fails[0]["content"]
    assert "Traceback" not in fails[0]["content"]


def test_error_line_containing_raise_mid_message_is_a_lesson(session_dir):
    """A real error whose prose happens to say 'raise' is not noise."""
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "bash",
                     "content": "RuntimeError: please raise the limit before retrying"}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    fails = [c for c in cands if "Observed failure" in c["content"]]
    assert len(fails) == 1
    assert "RuntimeError" in fails[0]["content"]


def test_latest_session_ignores_sidecars(session_dir):
    """__advisor.jsonl and changes.jsonl are not session logs."""
    (session_dir / "2026-11-18T08-00-00-000Z_a.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _session_entries()), encoding="utf-8")
    (session_dir / "__advisor.jsonl").write_text("{}", encoding="utf-8")
    (session_dir / "changes.jsonl").write_text("{}", encoding="utf-8")
    picked = capture.latest_session()
    assert picked is not None
    assert picked.name == "2026-11-18T08-00-00-000Z_a.jsonl"


# --- secret scanning in the auto-capture pipeline ---------------------------

def test_capture_never_stores_secret_looking_tool_output(session_dir):
    """Tool output with a key in it is dropped, not written to memory.

    The remember path scans content before storing; auto-capture read raw
    toolResult bodies and would persist a token from a traceback or env dump.
    """
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "bash",
                     "content": "Traceback... auth failed: sk-abcdefghijklmnop1234"}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_a.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    assert cands == []
    for c in cands:
        assert "sk-abcdefghijklmnop1234" not in c["content"]


def test_scan_text_shared_by_cli_and_capture():
    """One pattern table: cli import and capture module agree."""
    from omp_memory import cli
    assert cli.scan_text is capture.scan_text
    findings = capture.scan_text("password=hunter22")
    assert findings and findings[0][0] == "assignment"


def test_self_echo_tool_output_is_not_a_lesson(session_dir):
    """A toolResult echoing a past capture line is not a new failure.

    Capture output can land back in the session log (an agent printing
    candidates, a benchmark run); without this guard each run nests one
    more 'Observed failure' level into the store.
    """
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "toolResult", "toolName": "bash",
                     "content": "bash: - INFERENCE : Observed failure: "
                                "bash: FileNotFoundError: [Errno 2] No such file"}},
    ]
    _write_session(session_dir, "2026-11-18T08-00-00-000Z_echo.jsonl", entries)
    p = session_dir / "2026-11-18T08-00-00-000Z_echo.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    assert [c for c in cands if "Observed failure" in c["content"]] == []
