"""End-to-end tests for the MCP server (src/omp_memory/mcp.py).

A real server process is spawned over stdio, spoken to as an MCP client would,
and the database it wrote is inspected afterwards. This is the test that keeps
the server from becoming an inert file again.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


class Server:
    """A spawned stdio MCP server bound to a temp database."""

    def __init__(self, tmp_path, env_extra=None):
        self.db_path = tmp_path / "memory.db"
        env = {
            "PATH": os.environ.get("PATH", ""),
            # Only the repo src: the machine has a global PYTHONPATH that would
            # otherwise shadow it.
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

    def send_raw(self, text):
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.req_id += 1
        msg = {"jsonrpc": "2.0", "id": self.req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg))
        return self.read_response()

    def read_response(self):
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


@pytest.fixture()
def server(tmp_path):
    s = Server(tmp_path)
    yield s
    s.close()


def test_handshake_and_tool_list(server):
    result = server.request("initialize", {})["result"]
    assert result["serverInfo"] == {"name": "honcho", "version": "2.0.0"}
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert "Never store secrets" in result["instructions"]

    # notifications get no response; ping does
    server.send_raw(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert server.request("ping")["result"] == {}

    tools = server.request("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["recall", "remember", "reinforce",
                                          "forget", "stats", "maintenance"]


def test_remember_recall_round_trip(server):
    stored = server.call("remember", {
        "content": "the flange is secured with three bolts, not four",
        "type": "FACT", "evidence": "service manual page 12",
        "project": "workshop",
    })
    assert "Stored FACT" in stored
    assert "confidence=0.60" in stored
    assert "(inserted)" in stored

    recalled = server.call("recall", {"query": "flange bolts", "project": "workshop"})
    assert "the flange is secured with three bolts" in recalled
    assert "evidence=service manual page 12" in recalled
    assert "id=" in recalled


def test_recall_respects_confidence_filter(server):
    server.call("remember", {"content": "weak hunch about the valve", "type": "ASSUMPTION",
                             "confidence": 0.2})
    server.call("remember", {"content": "valve part number is 4471", "type": "FACT",
                             "confidence": 0.95})
    filtered = server.call("recall", {"query": "valve", "min_confidence": 0.9})
    assert "4471" in filtered
    assert "weak hunch" not in filtered


def test_identity_merge_same_fact_two_provenances(server):
    server.call("remember", {
        "content": "deploy via capistrano, never touch the shared mount",
        "type": "RULE", "project": "ops", "source": "runbook",
    })
    stored = server.call("remember", {
        # Same statement, different spelling: casing and whitespace are folded.
        "content": "  Deploy  via capistrano, never touch the shared mount ",
        "type": "RULE", "project": "infra", "source": "teammate",
    })
    # One content identity: the first row is updated, not duplicated.
    assert "(updated)" in stored
    stats = json.loads(server.call("stats", {}))
    assert stats["RULE"] == 1


def test_secret_refusal(server):
    out = server.call("remember", {"content": "key is sk-abcdefabcdefabcdefabcdef12"})
    assert "refused" in out
    stats = json.loads(server.call("stats", {}))
    assert stats["total"] == 0


def test_invalid_input_reported_not_raised(server):
    assert "invalid type" in server.call("remember", {"content": "x", "type": "NOTE"})
    assert "invalid signal" in server.call("reinforce", {"id": "nope", "signal": "up"})
    assert "reinforce:" in server.call("reinforce", {"id": "nope", "signal": "success"})
    assert "no record" in server.call("forget", {"id": "nope"})


def test_reinforce_forget_flow(server):
    stored = server.call("remember", {"content": "psi pump primes itself", "type": "FACT"})
    rid = stored.split("id=")[1].split()[0]
    out = server.call("reinforce", {"id": rid, "signal": "success", "source": "check"})
    assert "confidence 0.70" in out
    assert server.call("forget", {"id": rid}) == f"Forgot {rid} (FACT)"
    stats = json.loads(server.call("stats", {}))
    assert stats["total"] == 0
    assert stats["forgotten"] == 1


def test_batch_request(tmp_path):
    s = Server(tmp_path)
    try:
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "stats", "arguments": {}}},
        ]
        s.send_raw(json.dumps(batch))
        out = json.loads(s.proc.stdout.readline())
        # notification dropped, two responses kept, order preserved
        assert [r["id"] for r in out] == [1, 2]
        assert json.loads(out[1]["result"]["content"][0]["text"])["total"] == 0
    finally:
        s.close()


def test_unknown_method_and_tool(server):
    assert server.request("tools/bogus")["error"]["code"] == -32601
    resp = server.request("tools/call", {"name": "nope", "arguments": {}})
    assert resp["error"]["code"] == -32602


def test_db_lands_where_expected(server, tmp_path):
    server.call("remember", {"content": "persisted somewhere real", "type": "FACT"})
    server.proc.stdin.close()  # graceful exit so WAL is checkpointed
    server.proc.wait(timeout=10)
    assert (tmp_path / "memory.db").exists()


def test_resolve_db_path_per_user(tmp_path, monkeypatch):
    """Token identities get their own hashed database, never the shared one."""
    monkeypatch.setenv("HONCHO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "decoy.db"))
    monkeypatch.setenv("PYTHONPATH", str(SRC))
    sys.path.insert(0, str(SRC))
    try:
        from omp_memory import mcp
    finally:
        sys.path.pop(0)

    shared = mcp.resolve_db_path("")
    assert shared == tmp_path / "decoy.db"
    a = mcp.resolve_db_path("token-a")
    b = mcp.resolve_db_path("token-b")
    assert a != b and a != shared
    assert a.parent.parent.name == "users"
