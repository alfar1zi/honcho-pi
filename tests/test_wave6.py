"""Wave 6 tests: reinforcement feedback from recalled memory usage.

- recall exposes record ids so the agent can reinforce what it used.
- reinforce without an id targets the last single-hit recall.
- reinforce without an id after a multi-hit recall asks for the id.
- reinforce without any prior recall asks for the id.
- A recall-feedback success raises confidence and heat of the used record;
  decay and forget_cold still age records that are never used.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omp_memory import db

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


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


def _seed_records(srv: Server, n: int = 2) -> list[str]:
    ids = []
    for i in range(n):
        out = srv.call("remember", {
            "content": f"unique wave six fact number {i} about deployment",
            "type": "FACT",
        })
        ids.append(out.split("id=")[1].split()[0])
    return ids


def test_recall_output_carries_ids(tmp_path):
    srv = Server(tmp_path)
    try:
        srv.request("initialize", {})
        ids = _seed_records(srv, 1)
        out = srv.call("recall", {"query": "deployment fact"})
        assert f"id={ids[0]}" in out
        assert "reinforce {id, signal}" in out
    finally:
        srv.close()


def test_reinforce_without_id_uses_last_single_recall(tmp_path):
    srv = Server(tmp_path)
    try:
        srv.request("initialize", {})
        (rid,) = _seed_records(srv, 1)
        srv.call("recall", {"query": "deployment fact"})
        out = srv.call("reinforce", {"signal": "success"})
        assert f"Reinforced {rid}" in out
        assert "confidence 0.70" in out
        import sqlite3
        c = sqlite3.connect(str(srv.db_path))
        conf = c.execute("SELECT confidence FROM records WHERE id=?", (rid,)).fetchone()[0]
        src = c.execute("SELECT source FROM reinforcement WHERE record_id=?",
                        (rid,)).fetchone()[0]
        c.close()
        assert abs(conf - 0.70) < 1e-9
        assert src == "recall"
    finally:
        srv.close()


def test_reinforce_without_id_multi_hit_asks_for_id(tmp_path):
    srv = Server(tmp_path)
    try:
        srv.request("initialize", {})
        _seed_records(srv, 2)
        srv.call("recall", {"query": "deployment fact"})
        out = srv.call("reinforce", {"signal": "success"})
        assert "id is required" in out
        assert "multiple" in out
    finally:
        srv.close()


def test_reinforce_without_id_no_prior_recall_asks_for_id(tmp_path):
    srv = Server(tmp_path)
    try:
        srv.request("initialize", {})
        out = srv.call("reinforce", {"signal": "success"})
        assert "id is required" in out
        assert "recall first" in out
    finally:
        srv.close()


def test_reinforce_schema_allows_missing_id(tmp_path):
    srv = Server(tmp_path)
    try:
        srv.request("initialize", {})
        tools = srv.request("tools/list", {})["result"]["tools"]
        spec = next(t for t in tools if t["name"] == "reinforce")
        assert spec["inputSchema"]["required"] == ["signal"]
    finally:
        srv.close()


def test_reinforce_failure_lowers_confidence(tmp_path):
    srv = Server(tmp_path)
    try:
        srv.request("initialize", {})
        (rid,) = _seed_records(srv, 1)
        srv.call("recall", {"query": "deployment fact"})
        srv.call("reinforce", {"signal": "failure"})
        import sqlite3
        c = sqlite3.connect(str(srv.db_path))
        conf = c.execute("SELECT confidence FROM records WHERE id=?", (rid,)).fetchone()[0]
        c.close()
        assert abs(conf - 0.50) < 1e-9
    finally:
        srv.close()


def test_unused_records_still_decay_and_forget(conn=None, tmp_path=None):
    """The feedback loop must not exempt untouched records from aging."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "memory.db")
        c = db.connect(path)
        db.init_db(c, path)
        db.remember(c, "INFERENCE", "stale guess from long ago", source="auto-capture",
                    confidence=0.16)
        rid = c.execute("SELECT id FROM records").fetchone()[0]
        # Age the record past the forget window.
        old = "2020-01-01T00:00:00+00:00"
        c.execute("UPDATE records SET created_at=?, updated_at=? WHERE id=?",
                  (old, old, rid))
        c.commit()
        report = db.forget_cold(c, apply=True)
        assert report.get("forgotten", 0) >= 1
        assert db.get_record(c, rid) is None
        c.close()
