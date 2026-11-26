import json
import os

import pytest

from omp_memory import db
from omp_memory import web


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setenv("OMP_MEMORY_DB", str(db_file))
    monkeypatch.setattr(db, "DB_PATH", str(db_file))
    monkeypatch.setattr(web, "GRAPH_FILE", tmp_path / "graph.json")
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


# --- record write-back -------------------------------------------------------

def test_update_record_writes_content_and_keeps_id(graph_env):
    rid = db.upsert_record(graph_env, "FACT", "old lesson text", session_id="s")
    updated = web._update_record(rid, {"content": "corrected lesson text", "evidence": "operator edit"})
    assert updated["id"] == rid
    assert updated["content"] == "corrected lesson text"
    row = db.get_record(graph_env, rid)
    assert row["content"] == "corrected lesson text"
    # FTS trigger keeps search in sync with the edit
    hits = db.search(graph_env, "corrected lesson")
    assert any(h["id"] == rid for h in hits)


def test_update_record_rejects_empty_and_missing(graph_env):
    rid = db.upsert_record(graph_env, "FACT", "some lesson", session_id="s")
    with pytest.raises(ValueError):
        web._update_record(rid, {"content": "   "})
    # NotFound, not KeyError: str(KeyError) is just the id, so every surface
    # printed "error: nonexistent". The handler maps NotFound to 404 and
    # still accepts KeyError for callers that raise it.
    with pytest.raises(db.NotFound):
        web._update_record("nonexistent", {"content": "x"})


def test_merge_fresh_keeps_cosmetics_and_note_nodes(graph_env):
    rid = db.upsert_record(graph_env, "FACT", "wal mode lesson", session_id="s")
    saved = web.build_graph()
    node = next(n for n in saved["nodes"] if n["id"] == f"mem-{rid}")
    node["position"] = {"x": 123.0, "y": -45.0}
    node["data"]["label"] = "operator label"
    node["data"]["customLabel"] = True
    node["data"]["image"] = "/media/abc.png"
    saved["nodes"].append({
        "id": "n-note", "type": "graphNode", "position": {"x": 1, "y": 2},
        "data": {"label": "sticky note", "type": "note"},
    })
    saved["edges"].append({"id": "e-manual", "source": "n-note", "target": f"mem-{rid}"})
    web._update_record(rid, {"content": "wal mode lesson, revised", "evidence": "operator edit"})
    merged = web._merge_fresh(saved)
    mnode = next(n for n in merged["nodes"] if n["id"] == f"mem-{rid}")
    assert mnode["position"] == {"x": 123.0, "y": -45.0}
    assert mnode["data"]["label"] == "operator label"
    assert mnode["data"]["image"] == "/media/abc.png"
    assert any(n["id"] == "n-note" for n in merged["nodes"])
    assert any(e["id"] == "e-manual" for e in merged["edges"])


# --- images ------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_store_image_from_path(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "MEDIA_DIR", tmp_path / "media")
    src = tmp_path / "shot.png"
    src.write_bytes(PNG)
    out = web._store_image({"path": str(src)})
    assert out["url"].startswith("/media/")
    assert (tmp_path / "media" / out["url"].split("/")[-1]).read_bytes() == PNG


def test_store_image_from_data_url(tmp_path, monkeypatch):
    import base64
    monkeypatch.setattr(web, "MEDIA_DIR", tmp_path / "media")
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    out = web._store_image({"dataUrl": url})
    assert (tmp_path / "media" / out["url"].split("/")[-1]).read_bytes() == PNG


def test_store_image_rejects_svg_fake_and_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "MEDIA_DIR", tmp_path / "media")
    svg = tmp_path / "x.svg"
    svg.write_text("<svg onload=alert(1)>")
    with pytest.raises(ValueError):
        web._store_image({"path": str(svg)})
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"#!/bin/sh\necho pwned")
    with pytest.raises(ValueError):
        web._store_image({"path": str(fake)})
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (8 * 1024 * 1024))
    with pytest.raises(ValueError):
        web._store_image({"path": str(big)})
    with pytest.raises(ValueError):
        web._store_image({"dataUrl": "data:image/svg+xml;base64,PHN2Zz4="})


# --- dashboard stats ---------------------------------------------------------

def test_dashboard_stats_real_numbers(graph_env):
    db.upsert_record(graph_env, "FACT", "hot lesson", confidence=0.9, session_id="s")
    db.upsert_record(graph_env, "INFERENCE", "cold lesson", confidence=0.2,
                     source="auto-capture", session_id="s")
    stats = web._dashboard_stats()
    assert stats["store"]["total"] == 2
    assert stats["confidence"]["high"] == 1
    assert stats["confidence"]["low"] == 1
    assert stats["auto_captured"] == 1
    assert stats["graph"]["nodes"] >= 2
    assert stats["activity"], "created today must appear in activity"


def test_merge_fresh_content_edit_updates_default_label(graph_env):
    rid = db.upsert_record(graph_env, "FACT", "original lesson first line", session_id="s")
    saved = web.build_graph()
    web._update_record(rid, {"content": "rewritten lesson first line", "evidence": "operator edit"})
    merged = web._merge_fresh(saved)
    node = next(n for n in merged["nodes"] if n["id"] == f"mem-{rid}")
    assert node["data"]["label"].startswith("rewritten lesson"), \
        "default label must follow store content after an edit"


def test_content_edit_requires_evidence_or_verified(graph_env):
    rid = db.upsert_record(graph_env, "FACT", "unevidenced lesson", session_id="s")
    with pytest.raises(ValueError, match="evidence or verified"):
        web._update_record(rid, {"content": "sneaky rewrite"})
    # verified records may be edited without new evidence
    vid = db.upsert_record(graph_env, "RULE", "verified rule", verified=1, session_id="s")
    out = web._update_record(vid, {"content": "verified rule, refined"})
    assert out["content"] == "verified rule, refined"


def test_content_edit_rejects_oversize(graph_env):
    rid = db.upsert_record(graph_env, "FACT", "small lesson", evidence="e", session_id="s")
    with pytest.raises(ValueError, match="20000"):
        web._update_record(rid, {"content": "x" * 20001})


def test_content_edit_logs_correction_and_supersedes(graph_env):
    rid = db.upsert_record(graph_env, "FACT", "original claim", evidence="e",
                           confidence=0.9, session_id="s")
    web._update_record(rid, {"content": "corrected claim"})
    row = graph_env.execute(
        "SELECT signal, source FROM reinforcement WHERE record_id=? ORDER BY ts DESC LIMIT 1",
        (rid,)).fetchone()
    assert row["signal"] == "correction"
    assert row["source"] == "web-ui"
    # hypercorrection: correction delta is positive (reconsolidated memory),
    # amplified 1.5x above the 0.7 confidence threshold, clamped at 1.0
    conf = graph_env.execute("SELECT confidence FROM records WHERE id=?", (rid,)).fetchone()[0]
    assert conf == 1.0


def test_env_web_dir_wins_without_dist(tmp_path, monkeypatch):
    # An explicit override must not fall through to the checkout when dist is
    # missing there: GRAPH_FILE would point at tracked repo files.
    import importlib
    empty = tmp_path / "no-dist"
    empty.mkdir()
    monkeypatch.setenv("OMP_MEMORY_WEB_DIR", str(empty))
    try:
        reloaded = importlib.reload(web)
        assert reloaded.WEB_DIR == empty
        assert reloaded.GRAPH_FILE == empty / "data" / "graph.json"
    finally:
        monkeypatch.delenv("OMP_MEMORY_WEB_DIR")
        importlib.reload(web)


def test_merge_fresh_drops_foreign_mem_ids_keeps_notes(graph_env):
    rid = db.upsert_record(graph_env, "FACT", "local lesson", session_id="s")
    saved = web.build_graph()
    saved["nodes"].append({
        "id": "mem-foreign999", "type": "graphNode", "position": {"x": 9, "y": 9},
        "data": {"label": "foreign record", "type": "FACT"},
    })
    saved["nodes"].append({
        "id": "n-note", "type": "graphNode", "position": {"x": 1, "y": 2},
        "data": {"label": "sticky note", "type": "note"},
    })
    saved["edges"].append({"id": "e-foreign", "source": "mem-foreign999", "target": f"mem-{rid}"})
    saved["edges"].append({"id": "e-note", "source": "n-note", "target": f"mem-{rid}"})
    merged = web._merge_fresh(saved)
    ids = {n["id"] for n in merged["nodes"]}
    assert "mem-foreign999" not in ids, "foreign record id must self-heal away"
    assert "n-note" in ids, "operator notes survive"
    eids = {e["id"] for e in merged["edges"]}
    assert "e-foreign" not in eids, "edges to dropped nodes go too"
    assert "e-note" in eids


# --- write-door hardening ----------------------------------------------------

def test_web_edit_rejects_secret_content(graph_env):
    rid = db.upsert_record(graph_env, "FACT", "clean lesson", evidence="e", session_id="s")
    with pytest.raises(ValueError, match="secret"):
        web._update_record(rid, {"content": "use api_key=sk-abcdefgh12345678 for auth"})
    assert db.get_record(graph_env, rid)["content"] == "clean lesson"


def test_sleep_pass_takes_backup(graph_env, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))
    db.upsert_record(graph_env, "FACT", "sleep backup lesson", session_id="s")
    report = db.sleep_pass(graph_env, apply=True)
    assert report.get("backup"), "sleep must produce a snapshot"
    assert os.path.exists(report["backup"])


def test_health_flags_stale_backups(graph_env, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))
    h = db.health(graph_env)
    assert h["backup_stale"] is True
    db.backup_db(db.db_path_of(graph_env))
    h2 = db.health(graph_env)
    assert h2["backup_stale"] is False
    assert h2["backup_count"] == 1


# --- HTTP handler: CSRF + shape validation -----------------------------------

@pytest.fixture
def http_server(graph_env, tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(web, "DIST_DIR", tmp_path / "dist")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<html></html>")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _req(url, method, body=None, headers=None):
    import urllib.request
    import urllib.error
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_cross_origin_write_rejected(http_server):
    status, body = _req(http_server + "/api/graph", "PUT",
                        {"nodes": [], "edges": []},
                        {"Origin": "https://evil.example", "Content-Type": "text/plain"})
    assert status == 403
    status, _ = _req(http_server + "/api/record", "POST", {"id": "x"},
                     {"Origin": "https://evil.example"})
    assert status == 403


def test_same_origin_and_no_origin_pass(http_server):
    host = http_server.replace("http://", "")
    status, _ = _req(http_server + "/api/graph", "PUT", {"nodes": [], "edges": []},
                     {"Origin": http_server})
    assert status == 200
    status, _ = _req(http_server + "/api/graph", "PUT", {"nodes": [], "edges": []})
    assert status == 200


def test_put_graph_rejects_bad_shape(http_server):
    status, _ = _req(http_server + "/api/graph", "PUT", ["not", "a", "graph"])
    assert status == 400
    status, _ = _req(http_server + "/api/graph", "PUT", {"nodes": "x", "edges": []})
    assert status == 400


def test_load_graph_rebuilds_after_other_process_write(graph_env, tmp_path):
    db.upsert_record(graph_env, "FACT", "first lesson", session_id="s")
    graph_env.commit()
    g1 = web._load_graph()
    n1 = len([n for n in g1["nodes"] if n["id"].startswith("mem-")])
    # Another process (the capture timer) writes; the viewer must notice
    # on the next load without any UI edit.
    other = db.connect(str(tmp_path / "memory.db"))
    try:
        db.upsert_record(other, "FACT", "second lesson from timer", session_id="s")
        other.commit()
    finally:
        other.close()
    g2 = web._load_graph()
    n2 = len([n for n in g2["nodes"] if n["id"].startswith("mem-")])
    assert n2 == n1 + 1
    # Stable afterwards: no rebuild loop when nothing changed.
    g3 = web._load_graph()
    assert len(g3["nodes"]) == len(g2["nodes"])


def test_build_graph_migrates_a_fresh_store(tmp_path, monkeypatch):
    """`pip install` then `overview` on a new machine used to crash here.

    build_graph() opened the file without init_db(), so /api/graph raised
    "no such table: records" inside the request thread and the handler died
    mid-response: the browser saw a dropped connection, not an error.
    """
    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("OMP_MEMORY_DB", str(fresh))
    graph = web.build_graph()
    assert graph["nodes"] is not None
    assert fresh.exists()
    # And the schema really is there, not just an empty graph.
    conn = db.connect(str(fresh))
    try:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='records'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
