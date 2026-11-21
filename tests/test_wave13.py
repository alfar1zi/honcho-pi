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
    assert v == 15


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
