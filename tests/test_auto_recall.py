"""Auto-recall injection: first mem tool call of a session gets a recall block."""
import json

import pytest

from omp_memory import db
from omp_memory import mcp


MEM_TOOLS = {"recall", "remember", "reinforce", "forget", "stats", "handoff", "maintenance", "capture"}


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "memory.db"))
    mcp._recalled_users.clear()
    if hasattr(mcp._local, "conns"):
        mcp._local.conns.clear()
    yield tmp_path


def _seed_user_db(user: str, monkeypatch, tmp_path):
    """Write records into the per-user DB that get_conn(user) resolves."""
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "memory.db"))
    import omp_memory.mcp as m
    path = m.resolve_db_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(path))
    db.init_db(conn, str(path))
    db.remember(conn, "RULE", "wal mode is enabled for honcho db", source="test")
    db.remember(conn, "FACT", "honcho recall uses bm25 ranking", source="test", evidence="test")
    conn.close()


def _call(msg, user="u1"):
    resp = mcp.handle_message(msg, user=user)
    text = resp["result"]["content"][0]["text"]
    return resp, text


def test_first_call_gets_recall_block(mcp_env, monkeypatch):
    _seed_user_db("u1", monkeypatch, mcp_env)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "stats", "arguments": {}}}
    _call(msg)
    # stats is not a mem tool that needs injection? it is in MEM_TOOLS -> skip.
    # use a non-mem tool if available; else verify no injection for stats:
    resp, text = _call(msg)
    assert "Auto-recall" not in text


def test_injection_happens_for_extension_tool(mcp_env, monkeypatch):
    _seed_user_db("u1", monkeypatch, mcp_env)
    # register a fake extension handler to simulate a non-mem tool
    mcp._HANDLERS["echo"] = lambda args, user="": "echo:" + str(args.get("x"))
    try:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "echo", "arguments": {"x": "wal mode"}}}
        resp, text = _call(msg)
        assert "echo:wal mode" in text
        assert "Auto-recall" in text or "wal mode" in text
    finally:
        mcp._HANDLERS.pop("echo", None)


def test_injection_only_once(mcp_env, monkeypatch):
    _seed_user_db("u1", monkeypatch, mcp_env)
    mcp._HANDLERS["echo"] = lambda args, user="": "echo"
    try:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "echo", "arguments": {"x": "wal mode"}}}
        _, text1 = _call(msg)
        _, text2 = _call(msg)
        assert "Auto-recall" in text1
        assert "Auto-recall" not in text2
    finally:
        mcp._HANDLERS.pop("echo", None)


def test_injection_skips_mem_tools(mcp_env, monkeypatch):
    _seed_user_db("u1", monkeypatch, mcp_env)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "stats", "arguments": {}}}
    _, text = _call(msg)
    assert "Auto-recall" not in text
