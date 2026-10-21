#!/usr/bin/env python3
"""
test_honcho.py - self-contained test suite. No pytest, no network, no deps.

    python3 test_honcho.py            # run everything
    python3 test_honcho.py -v         # verbose per-test output

Covers the failure modes that broke v1:
  * connect()/init_db() destroying the database
  * FTS index desyncing from records
  * reinforce() crashing on a misspelled constant
  * recall returning irrelevant rows when confidence is uniform
  * an agent never being able to reach memory at all (MCP handshake)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

RESULTS: list[tuple[str, bool, str]] = []


def test(fn):
    def wrapper():
        try:
            fn()
            RESULTS.append((fn.__name__, True, ""))
        except Exception:
            RESULTS.append((fn.__name__, False, traceback.format_exc()))
    wrapper.__name__ = fn.__name__
    return wrapper


class Sandbox:
    """Isolated temp data dir with its own DB and backups."""

    def __enter__(self):
        self.root = Path(tempfile.mkdtemp(prefix="honcho-test-"))
        self.db = self.root / "memory.db"
        os.environ["HONCHO_HOME"] = str(self.root)
        os.environ["OMP_MEMORY_DB"] = str(self.db)
        os.environ["OMP_MEMORY_BACKUP_DIR"] = str(self.root / "backups")
        import importlib
        import memory_db
        importlib.reload(memory_db)
        self.m = memory_db
        self.conn = self.m.connect(str(self.db))
        self.conn = self.m.init_db(self.conn, str(self.db))
        return self

    def __exit__(self, *exc):
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------- storage

@test
def test_schema_and_write():
    with Sandbox() as s:
        rid = s.m.upsert_record(s.conn, "FACT", "alpha beta gamma",
                                verified=1, confidence=0.9, source="t")
        assert rid, "no id returned"
        assert s.m.get_record(s.conn, rid)["content"] == "alpha beta gamma"


@test
def test_search_ranks_relevant_first():
    """Uniform confidence must not let an unrelated row win."""
    with Sandbox() as s:
        for i in range(20):
            s.m.upsert_record(s.conn, "RULE", f"unrelated macshell wallpaper note {i}",
                              verified=1, confidence=0.92, source="t")
        target = s.m.upsert_record(
            s.conn, "RULE", "APPEND_SYSTEM.md dibaca dari profil aktif",
            verified=1, confidence=0.92, source="t")
        hits = s.m.search(s.conn, "APPEND_SYSTEM profil", limit=3)
        assert hits, "no hits"
        assert hits[0]["id"] == target, f"wrong top hit: {hits[0]['content'][:40]}"


@test
def test_search_survives_hostile_query():
    """FTS operator characters must not raise or return junk."""
    with Sandbox() as s:
        s.m.upsert_record(s.conn, "FACT", "pool size tuning", source="t")
        for q in ['"unclosed', "a AND (b", "*", "::::", "OR OR", "- -", "x" * 200]:
            hits = s.m.search(s.conn, q, limit=3)
            assert isinstance(hits, list), f"query {q!r} broke search"


@test
def test_dedup_same_content_same_id():
    with Sandbox() as s:
        a = s.m.upsert_record(s.conn, "FACT", "same", source="t", project_id="p")
        b = s.m.upsert_record(s.conn, "FACT", "same", source="t", project_id="p")
        assert a == b
        n = s.conn.execute("SELECT count(*) FROM records").fetchone()[0]
        assert n == 1, f"duplicate rows: {n}"


@test
def test_fts_never_desyncs():
    with Sandbox() as s:
        for i in range(10):
            s.m.upsert_record(s.conn, "FACT", f"bullet item {i}", source="t")
        recs = s.conn.execute("SELECT count(*) FROM records").fetchone()[0]
        fts = s.conn.execute("SELECT count(*) FROM records_fts").fetchone()[0]
        assert recs == fts, f"desync records={recs} fts={fts}"
        s.m.upsert_record(s.conn, "FACT", "bullet item 3", source="t")
        fts2 = s.conn.execute("SELECT count(*) FROM records_fts").fetchone()[0]
        assert fts2 == fts, "update duplicated an fts row"


@test
def test_reinforce_all_signals():
    with Sandbox() as s:
        rid = s.m.upsert_record(s.conn, "INFERENCE", "guess", confidence=0.5, source="t")
        assert abs(s.m.reinforce(s.conn, rid, "success") - 0.6) < 1e-9
        assert abs(s.m.reinforce(s.conn, rid, "failure") - 0.5) < 1e-9
        assert abs(s.m.reinforce(s.conn, rid, "correction") - 0.65) < 1e-9
        assert abs(s.m.reinforce(s.conn, rid, "decay") - 0.6) < 1e-9
        # clamps
        for _ in range(20):
            s.m.reinforce(s.conn, rid, "success")
        assert s.m.get_record(s.conn, rid)["confidence"] == 1.0
        try:
            s.m.reinforce(s.conn, "nope", "success")
            raise AssertionError("missing record should raise")
        except ValueError:
            pass


@test
def test_decay_lowers_stale_unverified():
    with Sandbox() as s:
        rid = s.m.upsert_record(s.conn, "FACT", "stale claim", confidence=0.8, source="t")
        future = datetime.now(timezone.utc) + timedelta(days=60)
        changed = s.m.apply_decay(s.conn, half_life_days=30.0, now=future)
        assert changed >= 1
        assert s.m.get_record(s.conn, rid)["confidence"] < 0.5


@test
def test_decay_exempts_verified_and_rules():
    with Sandbox() as s:
        v = s.m.upsert_record(s.conn, "FACT", "verified", verified=1, confidence=0.8, source="t")
        r = s.m.upsert_record(s.conn, "RULE", "rule", confidence=0.8, source="t")
        future = datetime.now(timezone.utc) + timedelta(days=365)
        s.m.apply_decay(s.conn, now=future)
        assert s.m.get_record(s.conn, v)["confidence"] == 0.8
        assert s.m.get_record(s.conn, r)["confidence"] == 0.8


@test
def test_project_scoping_isolates():
    with Sandbox() as s:
        s.m.upsert_record(s.conn, "FACT", "secret project alpha", source="t", project_id="alpha")
        assert s.m.search(s.conn, "secret", project_id="alpha")
        assert not s.m.search(s.conn, "secret", project_id="beta")


# ---------------------------------------------------------------- recovery

@test
def test_corruption_is_quarantined_not_deleted():
    with Sandbox() as s:
        for i in range(20):
            s.m.upsert_record(s.conn, "FACT", f"knowledge {i}", verified=1,
                              confidence=0.9, source="t")
        s.conn.close()
        s.m.backup_db(str(s.db))

        size = s.db.stat().st_size
        with open(s.db, "r+b") as f:
            for off in (100, 4096 * 2, 4096 * 5):
                if off < size - 1000:
                    f.seek(off)
                    f.write(b"\x00\xff" * 400)

        conn = s.m.connect(str(s.db))
        conn = s.m.init_db(conn, str(s.db))
        n = conn.execute("SELECT count(*) FROM records").fetchone()[0]
        assert n == 20, f"lost data after recovery: {n} rows"

        quarantined = list(s.root.glob("memory.db.corrupt.*"))
        assert quarantined, "corrupt file was not preserved"
        for p in quarantined:
            p.unlink()
        s.conn = conn


@test
def test_healthy_db_untouched_on_open():
    with Sandbox() as s:
        s.m.upsert_record(s.conn, "FACT", "keep me", source="t")
        conn = s.m.connect(str(s.db))
        conn = s.m.init_db(conn, str(s.db))
        assert conn.execute("SELECT count(*) FROM records").fetchone()[0] == 1
        conn.close()


# ---------------------------------------------------------------- MCP

def _mcp_session(env_extra: dict) -> subprocess.Popen:
    env = {**os.environ, **env_extra}
    return subprocess.Popen(
        [sys.executable, str(HERE / "honcho_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env,
    )


def _rpc(p: subprocess.Popen, method: str, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    p.stdin.write(json.dumps(msg) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    return json.loads(line) if line else None


@test
def test_mcp_handshake_and_tools():
    root = tempfile.mkdtemp(prefix="honcho-mcp-")
    try:
        p = _mcp_session({"HONCHO_HOME": root, "OMP_MEMORY_DB": ""})
        init = _rpc(p, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        assert init["result"]["serverInfo"]["name"] == "honcho"
        tools = _rpc(p, "tools/list", {}, 2)
        names = {t["name"] for t in tools["result"]["tools"]}
        assert names == {"recall", "remember", "reinforce", "forget", "stats"}, names
        p.stdin.close()
        p.terminate()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_mcp_full_cycle():
    root = tempfile.mkdtemp(prefix="honcho-mcp-")
    try:
        p = _mcp_session({"HONCHO_HOME": root, "OMP_MEMORY_DB": ""})
        _rpc(p, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = _rpc(p, "tools/call", {"name": "remember", "arguments": {
            "content": "unique_needle_xyz lives in project alpha",
            "type": "FACT", "verified": True, "confidence": 0.9}}, 2)
        text = r["result"]["content"][0]["text"]
        assert "Stored FACT" in text, text
        rid = text.split("id=")[1].split()[0]

        r = _rpc(p, "tools/call", {"name": "recall",
                                   "arguments": {"query": "unique_needle_xyz"}}, 3)
        assert "unique_needle_xyz" in r["result"]["content"][0]["text"]

        r = _rpc(p, "tools/call", {"name": "reinforce",
                                   "arguments": {"id": rid, "signal": "success"}}, 4)
        assert "confidence" in r["result"]["content"][0]["text"]

        r = _rpc(p, "tools/call", {"name": "forget", "arguments": {"id": rid}}, 5)
        assert "Forgot" in r["result"]["content"][0]["text"]

        r = _rpc(p, "tools/call", {"name": "recall",
                                   "arguments": {"query": "unique_needle_xyz"}}, 6)
        assert "No memory matched" in r["result"]["content"][0]["text"]
        p.stdin.close()
        p.terminate()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_mcp_refuses_secrets():
    root = tempfile.mkdtemp(prefix="honcho-mcp-")
    try:
        p = _mcp_session({"HONCHO_HOME": root, "OMP_MEMORY_DB": ""})
        _rpc(p, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = _rpc(p, "tools/call", {"name": "remember", "arguments": {
            "content": "key sk-abcdefghijklmnopqrstuvwxyz012345"}}, 2)
        assert "refused" in r["result"]["content"][0]["text"].lower()
        p.stdin.close()
        p.terminate()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_mcp_memory_persists_across_processes():
    """The whole point: a new session sees what the last one learned."""
    root = tempfile.mkdtemp(prefix="honcho-mcp-")
    env = {"HONCHO_HOME": root, "OMP_MEMORY_DB": ""}
    try:
        p = _mcp_session(env)
        _rpc(p, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        _rpc(p, "tools/call", {"name": "remember", "arguments": {
            "content": "cross_process_fact_42", "type": "FACT", "confidence": 0.9}}, 2)
        p.stdin.close()
        p.terminate()
        p.wait(timeout=10)

        p2 = _mcp_session(env)
        _rpc(p2, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = _rpc(p2, "tools/call", {"name": "recall",
                                    "arguments": {"query": "cross_process_fact_42"}}, 3)
        assert "cross_process_fact_42" in r["result"]["content"][0]["text"]
        p2.stdin.close()
        p2.terminate()
    finally:
        shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- migration

@test
def test_migrate_md_is_idempotent():
    root = tempfile.mkdtemp(prefix="honcho-mig-")
    try:
        md = Path(root) / "learned.md"
        md.write_text(
            "- first lesson about omp config\n"
            "- second lesson\n"
            "  continued on an indented line\n"
            "- third verified fact (diverifikasi)\n",
            encoding="utf-8")
        dbp = str(Path(root) / "m.db")
        env = {**os.environ, "OMP_MEMORY_DB": dbp,
               "OMP_MEMORY_BACKUP_DIR": str(Path(root) / "bak")}
        r1 = subprocess.run([sys.executable, str(HERE / "migrate_md.py"), str(md), "--db", dbp],
                            capture_output=True, text=True, env=env)
        assert r1.returncode == 0, r1.stderr
        r2 = subprocess.run([sys.executable, str(HERE / "migrate_md.py"), str(md), "--db", dbp],
                            capture_output=True, text=True, env=env)
        assert "inserted 0 new" in r2.stdout, r2.stdout
        c = sqlite3.connect(dbp)
        n = c.execute("SELECT count(*) FROM records").fetchone()[0]
        c.close()
        assert n == 3, f"expected 3 records, got {n}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    verbose = "-v" in sys.argv
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        if verbose:
            print(f"  {t.__name__} ...", end=" ", flush=True)
        t()
        if verbose and RESULTS and RESULTS[-1][0] == t.__name__:
            print("ok" if RESULTS[-1][1] else "FAIL")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{passed}/{len(RESULTS)} tests passed")
    for name, ok, tb in failed:
        print(f"\n=== FAIL {name} ===\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
