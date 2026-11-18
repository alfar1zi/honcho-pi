"""Wave I: Experience layer (cross-trajectory abstraction).

Covers the survey's Storage -> Reflection -> Experience arc: capture (Wave F)
already stores trajectories; reflect (this wave) evaluates them into
REFLECTIVE/PROCEDURAL/RULE knowledge and links episodes that belong to the
same recurring situation. ReasoningBank (arXiv 2509.25140) informs the
contrastive pair step: failures next to their successful counterpart become
a lesson, not just a log line.
"""
import json
import sqlite3

import pytest

from omp_memory import db
from omp_memory import experience


def _init(tmp_path):
    conn = db.connect(str(tmp_path / "m.db"))
    db.init_db(conn)
    return conn


def _remember(conn, rtype, content, project_id="", session_id="", confidence=0.6,
              verified=0, tags="", source="test", event_id="",
              created_at=None):
    rid = db.upsert_record(conn, rtype, content, evidence="x", verified=verified,
                           confidence=confidence, source=source, tags=tags,
                           session_id=session_id, project_id=project_id,
                           event_id=event_id)
    if created_at:
        conn.execute("UPDATE records SET created_at=? WHERE id=?",
                     (created_at, rid))
        conn.commit()
    return rid


# ---------------------------------------------------------------------------
# experience.reflect: cross-trajectory abstraction
# ---------------------------------------------------------------------------

def test_reflect_builds_procedural_from_repeated_rule_across_projects(tmp_path):
    """3+ records, 2+ projects, same topic -> PROCEDURAL with parent_ids."""
    conn = _init(tmp_path)
    _remember(conn, "RULE", "always add PYTHONPATH= before pytest runs",
              project_id="honcho-pi", session_id="s1")
    _remember(conn, "RULE", "pytest needs PYTHONPATH= prefix or venv shadowing",
              project_id="other-repo", session_id="s2")
    _remember(conn, "INFERENCE", "pytest without PYTHONPATH= prefix imports wrong",
              project_id="third-repo", session_id="s3")
    report = experience.reflect(conn, apply=True)
    assert report["procedural"] >= 1
    rows = conn.execute(
        "SELECT * FROM records WHERE type='PROCEDURAL'").fetchall()
    assert len(rows) >= 1
    parents = [p for p in rows[0]["parent_id"].split(",") if p]
    assert len(parents) >= 3


def test_reflect_needs_cross_project_spread(tmp_path):
    """Same-topic records in one project only: no experience written."""
    conn = _init(tmp_path)
    for i in range(4):
        _remember(conn, "RULE", f"rule about pytest and PYTHONPATH variant {i}",
                  project_id="honcho-pi", session_id=f"s{i}")
    before = conn.execute("SELECT COUNT(*) c FROM records WHERE type='PROCEDURAL'"
                          ).fetchone()["c"]
    experience.reflect(conn, apply=True)
    after = conn.execute("SELECT COUNT(*) c FROM records WHERE type='PROCEDURAL'"
                         ).fetchone()["c"]
    assert after == before


def test_reflect_is_idempotent(tmp_path):
    conn = _init(tmp_path)
    _remember(conn, "RULE", "always add PYTHONPATH= before pytest runs",
              project_id="honcho-pi", session_id="s1")
    _remember(conn, "RULE", "pytest needs PYTHONPATH= prefix or venv shadowing",
              project_id="other-repo", session_id="s2")
    _remember(conn, "INFERENCE", "pytest without PYTHONPATH= prefix imports wrong",
              project_id="third-repo", session_id="s3")
    first = experience.reflect(conn, apply=True)
    second = experience.reflect(conn, apply=True)
    n1 = conn.execute("SELECT COUNT(*) c FROM records WHERE type='PROCEDURAL'"
                      ).fetchone()["c"]
    assert first["procedural"] >= 1
    assert second["procedural"] == 0
    assert n1 == 1


# ---------------------------------------------------------------------------
# experience.contrastive_pairs: success vs failure on the same topic
# ---------------------------------------------------------------------------

def test_contrastive_pairs_pairs_failure_with_success(tmp_path):
    """A failure record whose topic matches a success record -> REFLECTIVE."""
    conn = _init(tmp_path)
    fail = _remember(conn, "INFERENCE", "Observed failure: bash: exited with code 1 "
                                        "when running pytest without PYTHONPATH",
                     project_id="honcho-pi", session_id="s1",
                     tags="failure,auto-capture")
    _remember(conn, "FACT", "pytest works when run with PYTHONPATH= prefix",
              project_id="honcho-pi", session_id="s2", verified=1)
    pairs = experience.contrastive_pairs(conn)
    assert len(pairs) >= 1
    assert pairs[0]["failure_id"] == fail
    assert pairs[0]["success_id"]


def test_contrastive_pairs_needs_failure_tag(tmp_path):
    conn = _init(tmp_path)
    _remember(conn, "INFERENCE", "Observed failure: bash: exited with code 1 "
                                 "when running pytest without PYTHONPATH",
              project_id="honcho-pi", session_id="s1")
    _remember(conn, "FACT", "pytest works when run with PYTHONPATH= prefix",
              project_id="honcho-pi", session_id="s2", verified=1)
    assert experience.contrastive_pairs(conn) == []


def test_reflect_writes_reflective_from_pair(tmp_path):
    conn = _init(tmp_path)
    _remember(conn, "INFERENCE", "Observed failure: bash: exited with code 1 "
                                 "when running pytest without PYTHONPATH",
              project_id="honcho-pi", session_id="s1",
              tags="failure,auto-capture")
    _remember(conn, "FACT", "pytest works when run with PYTHONPATH= prefix",
              project_id="honcho-pi", session_id="s2", verified=1)
    report = experience.reflect(conn, apply=True)
    assert report["reflective"] >= 1
    rows = conn.execute(
        "SELECT * FROM records WHERE type='REFLECTIVE'").fetchall()
    assert len(rows) >= 1
    assert "PYTHONPATH" in rows[0]["content"]


# ---------------------------------------------------------------------------
# experience.event_links: link related events across sessions/projects
# ---------------------------------------------------------------------------

def test_event_links_connect_similar_events(tmp_path):
    """Two events (different sessions) sharing >= threshold similarity link."""
    conn = _init(tmp_path)
    a = _remember(conn, "INFERENCE", "honcho-pi recall rerank uses bm25 lexical heat",
                  project_id="honcho-pi", session_id="s1",
                  tags="failure,auto-capture,event:0", event_id="s1#e0")
    _remember(conn, "INFERENCE", "honcho-pi rerank combines bm25 and heat scores",
              project_id="honcho-pi", session_id="s2",
              tags="failure,auto-capture,event:0", event_id="s2#e0")
    links = experience.event_links(conn)
    assert any({l["a"], l["b"]} == {"s1#e0", "s2#e0"} for l in links)


def test_event_links_dedup(tmp_path):
    conn = _init(tmp_path)
    _remember(conn, "INFERENCE", "honcho-pi recall rerank uses bm25 lexical heat",
              project_id="honcho-pi", session_id="s1",
              tags="failure,auto-capture,event:0", event_id="s1#e0")
    _remember(conn, "INFERENCE", "honcho-pi rerank combines bm25 and heat scores",
              project_id="honcho-pi", session_id="s2",
              tags="failure,auto-capture,event:0", event_id="s2#e0")
    links = experience.event_links(conn)
    seen = set()
    for l in links:
        key = frozenset((l["a"], l["b"]))
        assert key not in seen
        seen.add(key)


# ---------------------------------------------------------------------------
# experience.patterns: repeated pattern detection (good/bad/ugly)
# ---------------------------------------------------------------------------

def test_patterns_flags_repeated_failure_and_rule(tmp_path):
    conn = _init(tmp_path)
    for i in range(3):
        _remember(conn, "INFERENCE", f"Observed failure: bash exited with code 1 "
                                     f"pytest variant {i} without PYTHONPATH",
                  project_id=f"p{i}", session_id=f"s{i}",
                  tags="failure,auto-capture")
    _remember(conn, "RULE", "always prefix pytest with PYTHONPATH=",
              project_id="p0", session_id="s9")
    report = experience.reflect(conn, apply=True)
    assert report["patterns"]
    kinds = {p["kind"] for p in report["patterns"]}
    assert "recurring_failure" in kinds
    assert "recurring_rule" in kinds


# ---------------------------------------------------------------------------
# maintenance wiring: op=reflect
# ---------------------------------------------------------------------------

def test_maintenance_reflect_op(tmp_path, monkeypatch):
    from omp_memory import mcp
    monkeypatch.setattr(mcp, "resolve_db_path", lambda user: str(tmp_path / "m.db"))
    conn = _init(tmp_path)
    monkeypatch.setattr(mcp, "get_conn", lambda user: conn)
    _remember(conn, "RULE", "always add PYTHONPATH= before pytest runs",
              project_id="honcho-pi", session_id="s1")
    _remember(conn, "RULE", "pytest needs PYTHONPATH= prefix or venv shadowing",
              project_id="other-repo", session_id="s2")
    _remember(conn, "INFERENCE", "pytest without PYTHONPATH= prefix imports wrong",
              project_id="third-repo", session_id="s3")
    out = json.loads(mcp.tool_maintenance({"op": "reflect"}))
    assert "procedural" in out and "reflective" in out
    assert out["procedural"] >= 1
