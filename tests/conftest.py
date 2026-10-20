"""Shared fixtures: isolated DB per test, no shared state."""
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from omp_memory import db as memory_db  # noqa: E402


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Isolated DB path inside a per-test tmp dir; env points at it."""
    path = tmp_path / "memory" / "memory.db"
    monkeypatch.setenv("OMP_MEMORY_DB", str(path))
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "memory" / "backups"))
    # module constants are read at import time; rebind for the test
    monkeypatch.setattr(memory_db, "DB_PATH", str(path))
    monkeypatch.setattr(memory_db, "BACKUP_DIR", str(tmp_path / "memory" / "backups"))
    return path


@pytest.fixture()
def conn(db_path):
    c = memory_db.connect(str(db_path))
    memory_db.init_db(c)
    yield c
    c.close()
