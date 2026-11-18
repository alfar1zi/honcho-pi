"""Benchmark harness: read-only, 6 question types, works on any corpus."""
import subprocess
import sys
from pathlib import Path

import importlib.util

from omp_memory import db as memory_db

BENCH = Path(__file__).resolve().parent.parent / "bench" / "run.py"

_spec = importlib.util.spec_from_file_location("bench_run", BENCH)
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)


def _seed(conn):
    memory_db.remember(conn, "FACT", "deploy pipeline uses docker buildx",
                       confidence=0.8, verified=1)
    memory_db.remember(conn, "FACT", "deploy pipeline uses kaniko builds",
                       confidence=0.8, verified=1)
    memory_db.remember(conn, "FACT", "the grafana port is 3300", confidence=0.7)
    for i in range(6):
        memory_db.remember(conn, "INFERENCE", f"pipeline note number {i} about deploys",
                           confidence=0.5)


def test_bench_runs_readonly_and_scores(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bench.db")
    conn = memory_db.connect(db_path)
    memory_db.init_db(conn)
    _seed(conn)
    conn.close()

    proc = subprocess.run(
        [sys.executable, str(BENCH), "--db", db_path],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for label in ("exact", "update", "temporal", "abstain", "join", "topical",
                  "overall"):
        assert label in out
    # abstain probes are nonsense tokens: the harness must expect 100% there
    abstain_line = [l for l in out.splitlines() if l.startswith("abstain")][0]
    assert "3/3" in abstain_line


def test_supersede_probe_measures_downrank(tmp_path):
    """The probe must produce a case from a real correction chain and expect
    the replacement at the top: a probe that never fires proves nothing."""
    db_path = str(tmp_path / "supersede.db")
    conn = memory_db.connect(db_path)
    memory_db.init_db(conn)
    rid = memory_db.remember(conn, "FACT",
                             "deploy host is prod-01 in us-east")["id"]
    memory_db.remember(conn, "FACT", "deploy host is prod-01 in us-west")
    memory_db.reinforce(conn, rid, "correction",
                        content="deploy host is prod-01 in us-central",
                        evidence="ops confirmed")
    rows = run.probe_rows(conn, 60)
    cases = run.question_supersede(rows)
    assert len(cases) == 1
    query, expect = cases[0]
    hits = memory_db.recall(conn, query, limit=5)
    assert hits[0]["id"] == expect
    conn.close()


def test_bench_refuses_write(tmp_path, monkeypatch):
    # mode=ro: even a corrupt-friendly run cannot touch the file
    db_path = str(tmp_path / "bench.db")
    conn = memory_db.connect(db_path)
    memory_db.init_db(conn)
    _seed(conn)
    conn.close()
    before = Path(db_path).read_bytes()

    proc = subprocess.run(
        [sys.executable, str(BENCH), "--db", db_path],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(db_path).read_bytes() == before


def test_multisession_probe_scopes_recall(tmp_path):
    """The scoped query must pick the asked session's record even when the
    other session's record is the stronger unscoped match. A probe whose
    target already wins without the filter proves nothing about scoping."""
    db_path = str(tmp_path / "multi.db")
    conn = memory_db.connect(db_path)
    memory_db.init_db(conn)
    # sess-b carries the query words with higher confidence: it wins the
    # unscoped ranking, so only the session filter can put sess-a on top
    b = memory_db.remember(conn, "FACT", "auth links magic tenant codes rotated",
                           session_id="sess-b", confidence=0.9, verified=1)
    a = memory_db.remember(conn, "FACT", "auth links magic tenant alpha note",
                           session_id="sess-a", confidence=0.6)
    q = "auth links magic tenant"
    unscoped = memory_db.recall(conn, q, limit=3, full=True)
    assert [h["id"] for h in unscoped] == [b["id"], a["id"]]
    scoped = memory_db.recall(conn, q, limit=3, session_id="sess-a", full=True)
    assert [h["id"] for h in scoped] == [a["id"]]
    assert all(h["session_id"] == "sess-a" for h in scoped)
    conn.close()


def test_procedure_probe_needs_same_topic_competitor(tmp_path):
    """A PROCEDURAL record with no same-topic competitor emits no probe case.

    A probe that cannot fail without the type weight measures nothing.
    With a competitor sharing vocabulary, the case exists and the
    procedure record is the expected top hit.
    """
    conn = memory_db.connect(str(tmp_path / "m.db"))
    memory_db.init_db(conn)
    memory_db.remember(conn, "PROCEDURAL",
                       "rebuild the records check with rebuild_records_check swap",
                       verified=1)
    rows = run.probe_rows(conn, 60)
    assert run.question_procedure(rows) == []
    # competitor shares vocabulary but is not the procedure
    memory_db.remember(conn, "INFERENCE",
                       "the rebuild_records_check swap keeps the table name")
    rows = run.probe_rows(conn, 60)
    cases = run.question_procedure(rows)
    assert len(cases) == 1
    hits = memory_db.recall(conn, cases[0][0], limit=3)
    assert hits and hits[0]["id"] == cases[0][1]
    conn.close()


def test_get_conn_recovers_from_stale_schema(tmp_path, monkeypatch):
    """A cached connection pinned to a dead schema is reopened, not fatal.

    Observed live (2026-11-20): a server process that opened its connection
    before another process migrated the records table failed every later
    tool call with "malformed database schema" while fresh connections
    worked fine. The error lives in the connection's schema cache, not the
    file, so get_conn must drop and reopen on that signature.
    """
    import sqlite3
    from omp_memory import mcp
    key = str(tmp_path / "m.db")
    monkeypatch.setenv("OMP_MEMORY_DB", key)
    c1 = mcp.get_conn()

    # Simulate the observed failure: the cached handle now raises the
    # malformed-schema error on every statement (schema cache stale after
    # an external migration). Real sqlite does this after a foreign rebuild;
    # we poison the handle directly to test get_conn's recovery, not
    # sqlite's internals.
    class Poison:
        def __init__(self, inner):
            self._inner = inner

        def close(self):
            self._inner.close()

        def execute(self, sql, *a):
            if "records" in sql:
                raise sqlite3.DatabaseError("malformed database schema (RULE)")
            return self._inner.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    poisoned = Poison(c1)
    mcp._local.conns[key] = poisoned
    c2 = mcp.get_conn()
    assert c2 is not poisoned
    assert c2.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    # and the new handle is cached for later calls
    assert mcp.get_conn() is c2
