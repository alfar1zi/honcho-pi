"""Wave 13: digest block, per-record strength, schema v15."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from omp_memory import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "m.db"))
    db.init_db(c)
    yield c
    c.close()


def test_schema_v15_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(records)")}
    assert {"decay_tau", "valid_from", "valid_until",
            "anchor_path", "anchor_sha", "stale"} <= cols
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 16


def test_digest_budget_and_stability(conn):
    for i in range(30):
        db.remember(conn, "RULE", f"rule number {i} about workflow discipline",
                    verified=1, source="s")
    d1 = db.digest(conn, budget=500)
    d2 = db.digest(conn, budget=500)
    assert d1["text"] == d2["text"], "digest must be deterministic for prefix caches"
    assert d1["chars"] <= 500
    assert d1["records"] < d1["total_eligible"], "budget must actually cut"
    assert d1["text"].startswith("[memory digest")


def test_digest_prefers_verified_rules(conn):
    db.remember(conn, "FACT", "hot fact", confidence=0.95, source="s")
    db.remember(conn, "RULE", "verified rule wins", verified=1, confidence=0.6, source="s")
    d = db.digest(conn, budget=200)
    assert "verified rule wins" in d["text"]


def test_digest_excludes_superseded_and_expired(conn):
    r = db.remember(conn, "FACT", "old fact superseded", confidence=0.9, source="s")
    conn.execute("UPDATE records SET superseded_by='other' WHERE id=?", (r["id"],))
    conn.commit()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.remember(conn, "FACT", "expired fact", confidence=0.9, source="s")
    conn.execute("UPDATE records SET valid_until=? WHERE content='expired fact'", (past,))
    conn.commit()
    d = db.digest(conn, budget=5000)
    assert "old fact superseded" not in d["text"]
    assert "expired fact" not in d["text"]


def test_strength_grows_on_spaced_success(conn):
    r = db.remember(conn, "FACT", "strength lesson", confidence=0.5, source="s")
    rid = r["id"]
    # explicit delta bypasses spacing gate: strength grows
    db.reinforce(conn, rid, "success", delta=0.1)
    tau1 = conn.execute("SELECT decay_tau FROM records WHERE id=?", (rid,)).fetchone()[0]
    assert tau1 == pytest.approx(db.DECAY_POWER_TAU_DAYS * db.STRENGTH_GROWTH)
    db.reinforce(conn, rid, "success", delta=0.1)
    tau2 = conn.execute("SELECT decay_tau FROM records WHERE id=?", (rid,)).fetchone()[0]
    assert tau2 > tau1


def test_strength_shrinks_on_failure(conn):
    r = db.remember(conn, "FACT", "fragile lesson", confidence=0.5, source="s")
    rid = r["id"]
    db.reinforce(conn, rid, "failure")
    tau = conn.execute("SELECT decay_tau FROM records WHERE id=?", (rid,)).fetchone()[0]
    assert tau == pytest.approx(db.DECAY_POWER_TAU_DAYS * db.STRENGTH_SHRINK)


def test_massed_repeat_does_not_grow_strength(conn):
    r = db.remember(conn, "FACT", "crammed lesson", confidence=0.5, source="s")
    rid = r["id"]
    db.reinforce(conn, rid, "success")  # first: no prior, spacing 1.0
    tau1 = conn.execute("SELECT decay_tau FROM records WHERE id=?", (rid,)).fetchone()[0]
    db.reinforce(conn, rid, "success")  # immediate repeat: massed, 0.1x
    tau2 = conn.execute("SELECT decay_tau FROM records WHERE id=?", (rid,)).fetchone()[0]
    assert tau2 == tau1, "cramming must not lengthen the horizon"


def test_personal_tau_slows_decay(conn):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=20)).isoformat()
    r1 = db.remember(conn, "FACT", "strong old lesson", confidence=0.8, source="s")
    r2 = db.remember(conn, "FACT", "weak old lesson", confidence=0.8, source="s")
    conn.execute("UPDATE records SET decay_tau=100, updated_at=?, last_decay_at='' WHERE id=?",
                 (old, r1["id"]))
    conn.execute("UPDATE records SET updated_at=?, last_decay_at='' WHERE id=?",
                 (old, r2["id"]))
    conn.commit()
    db.apply_decay(conn, now=now)
    c1 = conn.execute("SELECT confidence FROM records WHERE id=?", (r1["id"],)).fetchone()[0]
    c2 = conn.execute("SELECT confidence FROM records WHERE id=?", (r2["id"],)).fetchone()[0]
    assert c1 > c2, "personal strength must slow the forgetting curve"


def test_strength_cap(conn):
    r = db.remember(conn, "FACT", "immortal attempt", confidence=0.5, source="s")
    for _ in range(20):
        db.reinforce(conn, r["id"], "success", delta=0.01)
    tau = conn.execute("SELECT decay_tau FROM records WHERE id=?", (r["id"],)).fetchone()[0]
    assert tau <= db.STRENGTH_TAU_MAX_DAYS


# --- Wave C: bi-temporal, conflicts, anchors ----------------------------------

def test_as_of_filters_validity(conn):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=10)).isoformat()
    mid = (now - timedelta(days=5)).isoformat()
    future = (now + timedelta(days=10)).isoformat()
    db.remember(conn, "FACT", "wal mode is the journal setting", confidence=0.9, source="s")
    conn.execute("UPDATE records SET valid_until=? WHERE content LIKE 'wal mode%'", (mid,))
    conn.commit()
    # as_of before expiry: visible; after: gone
    assert db.recall(conn, "wal mode journal", as_of=past)
    assert not db.recall(conn, "wal mode journal", as_of=future)
    # no as_of: expired record still returned (default = current truth incl. history)
    assert db.recall(conn, "wal mode journal")


def test_conflicts_surfaces_supersede_pair(conn):
    r1 = db.remember(conn, "FACT", "use pip for dependency installs always", confidence=0.8, source="s")
    r2 = db.remember(conn, "CORRECTION", "use uv for dependency installs always, pip is slow",
                     confidence=0.9, source="s")
    conn.execute("UPDATE records SET superseded_by=? WHERE id=?", (r2["id"], r1["id"]))
    conn.commit()
    cf = db.conflicts(conn, [r1["id"], r2["id"]])
    assert len(cf) == 1
    assert cf[0]["kind"] == "supersede"


def test_conflicts_ignores_unrelated(conn):
    r1 = db.remember(conn, "FACT", "sqlite wal mode journaling", source="s")
    r2 = db.remember(conn, "FACT", "react flow graph rendering", source="s")
    assert db.conflicts(conn, [r1["id"], r2["id"]]) == []


def test_check_anchors_flags_changed_file(conn, tmp_path):
    f = tmp_path / "config.py"
    f.write_text("TIMEOUT = 30\n")
    import hashlib
    sha = hashlib.sha256(f.read_bytes()).hexdigest()
    r = db.remember(conn, "FACT", "timeout is thirty seconds in config", source="s")
    conn.execute("UPDATE records SET anchor_path=?, anchor_sha=? WHERE id=?",
                 (str(f), sha, r["id"]))
    conn.commit()
    report = db.check_anchors(conn, apply=True)
    assert report["checked"] == 1 and not report["flagged"]
    f.write_text("TIMEOUT = 60\n")
    report = db.check_anchors(conn, apply=True)
    assert report["flagged"] == [r["id"]]
    assert conn.execute("SELECT stale FROM records WHERE id=?", (r["id"],)).fetchone()[0] == 1
    # restore the file: flag clears
    f.write_text("TIMEOUT = 30\n")
    report = db.check_anchors(conn, apply=True)
    assert report["cleared"] == [r["id"]]


# --- Wave D: anti-hoarding + write staging ------------------------------------

def test_env_failures_are_not_lessons():
    from omp_memory import capture
    entries = [
        {"type": "message", "message": {"role": "toolResult", "toolName": "bash",
         "content": "bash: uv: command not found"}},
        {"type": "message", "message": {"role": "toolResult", "toolName": "bash",
         "content": "Error: connect ECONNREFUSED 127.0.0.1:5432"}},
        {"type": "message", "message": {"role": "toolResult", "toolName": "bash",
         "content": "ModuleNotFoundError: No module named 'requests'"}},
    ]
    lessons = capture.extract_lessons(entries, None)
    assert not any("command not found" in l["content"] for l in lessons)
    assert not any("ECONNREFUSED" in l["content"] for l in lessons)


def test_unfixed_complaint_skipped_fixed_kept():
    from omp_memory import capture
    entries = [
        {"type": "message", "message": {"role": "toolResult", "toolName": "bash",
         "content": "AssertionError: the deploy script does not work"}},
        {"type": "message", "message": {"role": "toolResult", "toolName": "bash",
         "content": "AssertionError: parser is broken. fix: escape the quotes, resolved"}},
    ]
    lessons = capture.extract_lessons(entries, None)
    contents = " ".join(l["content"] for l in lessons)
    assert "does not work" not in contents
    assert "fix: escape" in contents or "resolved" in contents


def test_staging_approve_reject(conn):
    cand = {"rtype": "INFERENCE", "content": "staged lesson about wal checkpoints",
            "evidence": "observed", "confidence": 0.35, "source": "auto-capture",
            "tags": "failure", "session_id": "s", "project_id": "p", "event_id": ""}
    pid = db.stage_write(conn, cand)
    assert db.list_pending(conn)[0]["id"] == pid
    # nothing in records yet
    assert not db.recall(conn, "wal checkpoints staged")
    # duplicate staging is a no-op
    assert db.stage_write(conn, cand) == pid
    result = db.decide_pending(conn, pid, approve=True)
    assert result["status"] == "approved" and result["record_id"]
    assert any(not h.get("abstained") for h in db.recall(conn, "wal checkpoints staged"))
    # decided twice = noop
    assert db.decide_pending(conn, pid, approve=True)["noop"]
    pid2 = db.stage_write(conn, {**cand, "content": "another staged lesson reject me"})
    assert db.decide_pending(conn, pid2, approve=False)["status"] == "rejected"
    assert not any("reject me" in (h.get("snippet") or "")
                   for h in db.recall(conn, "reject me staged"))


def test_staging_hash_pin(conn):
    cand = {"rtype": "INFERENCE", "content": "pinned lesson content", "confidence": 0.35}
    pid = db.stage_write(conn, cand)
    conn.execute("UPDATE pending_writes SET content='tampered content' WHERE id=?", (pid,))
    conn.commit()
    with pytest.raises(ValueError, match="changed since staging"):
        db.decide_pending(conn, pid, approve=True)


# --- Wave E: PPR associative expansion ----------------------------------------

def test_ppr_spreads_mass_to_neighbors(conn):
    a = db.remember(conn, "FACT", "quokka migration uses zorbital checkpoints", source="s")
    b = db.remember(conn, "FACT", "zorbital checkpoints require wal flush ordering", source="s")
    c = db.remember(conn, "FACT", "completely unrelated cooking recipe pasta", source="s")
    rows = [db.get_record(conn, x["id"]) for x in (a, b, c)]
    ppr = db._ppr_expand(conn, [rows[0]], rows)
    assert ppr[b["id"]] > ppr[c["id"]], "shared rare term must carry mass"
    assert ppr[c["id"]] < 0.01


def test_ppr_parent_link(conn):
    p = db.remember(conn, "RULE", "parent rule about deployment", source="s")
    ch = db.remember(conn, "FACT", "child detail", source="s", parent_id=p["id"])
    rows = [db.get_record(conn, p["id"]), db.get_record(conn, ch["id"])]
    ppr = db._ppr_expand(conn, [rows[0]], rows)
    assert ppr[ch["id"]] > 0.05


def test_recall_still_lexical_lead_with_ppr(conn):
    db.remember(conn, "FACT", "the zorbital engine handles wal checkpoints", source="s")
    db.remember(conn, "FACT", "wal wal wal wal wal wal", source="s")
    hits = db.recall(conn, "zorbital engine", limit=2)
    assert hits and "zorbital" in hits[0]["snippet"]


# --- Wave F: skill export + sync pack -----------------------------------------

def test_export_skill_folder(conn, tmp_path):
    p = db.remember(conn, "PROCEDURAL", "deploy the gateway: build, sign, push, verify",
                    evidence="run 42 exit 0", source="s")
    db.remember(conn, "FACT", "episode one detail", source="s", parent_id=p["id"])
    db.remember(conn, "FACT", "episode two detail", source="s", parent_id=p["id"])
    out = db.export_skill(conn, p["id"], tmp_path)
    skill_md = tmp_path / out["name"] / "SKILL.md"
    text = skill_md.read_text()
    assert text.startswith("---\nname: ")
    assert "description: " in text
    assert len(out["references"]) == 2
    assert (tmp_path / out["name"] / "references" / "episode-1.md").exists()
    # description cap
    desc_line = next(l for l in text.splitlines() if l.startswith("description:"))
    assert len(desc_line) <= len("description: ") + 100


def test_export_skill_rejects_wrong_type(conn, tmp_path):
    f = db.remember(conn, "FACT", "not a procedure", source="s")
    with pytest.raises(ValueError):
        db.export_skill(conn, f["id"], tmp_path)


def test_pack_roundtrip_and_merge(conn, tmp_path):
    db.remember(conn, "FACT", "pack lesson alpha", confidence=0.6, source="s")
    db.remember(conn, "RULE", "pack rule beta", verified=1, confidence=0.9, source="s")
    pack = tmp_path / "team.pack"
    info = db.export_pack(conn, pack)
    assert info["records"] == 2
    # deterministic: same store, same bytes
    pack2 = tmp_path / "team2.pack"
    info2 = db.export_pack(conn, pack2)
    assert info["sha256"] == info2["sha256"]
    # import into a fresh store
    c2 = db.connect(str(tmp_path / "other.db"))
    db.init_db(c2)
    r = db.import_pack(c2, pack)
    assert r["imported"] == 2
    # re-import: no-op merge
    r2 = db.import_pack(c2, pack)
    assert r2["imported"] == 0 and r2["merged"] == 2
    # verification propagates: verify on machine 2, pack back, machine 1 takes max
    rid = db.recall(c2, "pack lesson alpha")[0]["id"]
    c2.execute("UPDATE records SET verified=1, confidence=0.95 WHERE id=?", (rid,))
    c2.commit()
    back = tmp_path / "back.pack"
    db.export_pack(c2, back)
    db.import_pack(conn, back)
    row = conn.execute("SELECT verified, confidence FROM records WHERE id=?", (rid,)).fetchone()
    assert row[0] == 1 and row[1] >= 0.95
    c2.close()
