"""Benchmark harness: read-only, 4 question types, works on any corpus."""
import subprocess
import sys
from pathlib import Path

from omp_memory import db as memory_db

BENCH = Path(__file__).resolve().parent.parent / "bench" / "run.py"


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
    for label in ("exact", "update", "temporal", "abstain", "overall"):
        assert label in out
    # abstain probes are nonsense tokens: the harness must expect 100% there
    abstain_line = [l for l in out.splitlines() if l.startswith("abstain")][0]
    assert "3/3" in abstain_line


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
