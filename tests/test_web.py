import json

import pytest

from omp_memory import db
from omp_memory import web


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setenv("OMP_MEMORY_DB", str(db_file))
    monkeypatch.setattr(db, "DB_PATH", str(db_file))
    conn = db.connect(str(db_file))
    db.init_db(conn)
    yield conn
    conn.close()


def test_build_graph_returns_nodes_and_edges(graph_env):
    db.upsert_record(graph_env, "FACT", "wal mode lesson", session_id="s")
    graph = web.build_graph()
    assert graph["nodes"], "records must produce nodes"
    mem_nodes = [n for n in graph["nodes"] if n["id"].startswith("mem-")]
    assert mem_nodes and all("position" in n for n in mem_nodes)


def test_graph_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "GRAPH_FILE", tmp_path / "graph.json")
    graph = {"nodes": [{"id": "a"}], "edges": []}
    web._save_graph(graph)
    assert web._load_graph() == graph


def test_skill_nodes_from_dirs(tmp_path, monkeypatch):
    managed = tmp_path / ".omp/agent/managed-skills"
    (managed / "alpha").mkdir(parents=True)
    (managed / "alpha" / "SKILL.md").write_text("---\ndescription: alpha skill\n---\nbody", encoding="utf-8")
    user = tmp_path / ".omp/agent/skills"
    (user / "beta").mkdir(parents=True)
    (user / "beta" / "SKILL.md").write_text("---\ndescription: beta\n---\nbody", encoding="utf-8")
    monkeypatch.setattr(web.Path, "home", classmethod(lambda cls: tmp_path))
    nodes = web._skill_nodes()
    ids = {n["id"] for n in nodes}
    assert "skill-managed-alpha" in ids
    assert "skill-user-beta" in ids
