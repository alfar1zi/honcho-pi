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
    _write_session(session_dir, "a.jsonl", _session_entries())
    p = session_dir / "a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    types = {c["rtype"] for c in cands}
    assert "INFERENCE" in types
    assert "ASSUMPTION" in types
    for c in cands:
        assert c["source"] == "auto-capture"
        assert c["confidence"] <= 0.5
        assert c["session_id"] == "a"
        assert c["project_id"] == "proj"


def test_extract_lessons_skips_control_text(session_dir):
    entries = [
        {"type": "message", "timestamp": "2026-11-18T08:00:00.000Z",
         "message": {"role": "user", "content":
                     "[System note] recalled memory context: decided to skip this"}},
        {"type": "message", "timestamp": "2026-11-18T08:00:01.000Z",
         "message": {"role": "user", "content": "real request"}},
    ]
    _write_session(session_dir, "a.jsonl", entries)
    p = session_dir / "a.jsonl"
    cands = capture.extract_lessons(capture._read_entries(p), p)
    for c in cands:
        assert "decided to skip this" not in c["content"]
    # The real user turn has no decision keyword, so no decision candidate.
    decision_cands = [c for c in cands if c["tags"].startswith("decision")]
    assert decision_cands == []


def test_capture_dry_run_writes_nothing(conn, session_dir):
    _write_session(session_dir, "a.jsonl", _session_entries())
    report = capture.capture_session(db, conn, apply=False)
    assert report["written"] == 0
    assert report["apply"] is False
    assert report["candidates"], "dry run must still report candidates"
    assert db.stats(conn)["total"] == 0


def test_capture_apply_writes_records(conn, session_dir):
    _write_session(session_dir, "a.jsonl", _session_entries())
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
    _write_session(session_dir, "a.jsonl", _session_entries())
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
    (sessions / "a.jsonl").write_text(
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
