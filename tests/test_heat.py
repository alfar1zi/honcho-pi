"""Heat, type promotion and cold forgetting.

Heat ranks what is worth keeping: confidence, recency and evidence of use.
Promotion is the only path that changes a record's type, and it needs evidence
from two independent sources, never a timestamp. Forgetting is gated on four
independent conditions and always leaves an audit row behind.
"""
from datetime import datetime, timedelta, timezone

import pytest

from omp_memory import db as memory_db


def _stamp(days_ago=0.0, hours_ago=0.0):
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def _age(conn, rid, days):
    conn.execute("UPDATE records SET updated_at=? WHERE id=?", (_stamp(days), rid))
    conn.commit()


def test_heat_parts_is_pure_and_reports_its_inputs():
    row = {"id": "x", "type": "FACT", "confidence": 0.5, "updated_at": _stamp(0)}
    parts = memory_db.heat_parts(row, [], datetime.now(timezone.utc))
    assert set(parts) >= {"heat", "confidence", "age_days", "recency",
                          "reinforcement", "successes", "net_delta", "sources"}
    assert parts["age_days"] == pytest.approx(0.0, abs=0.01)
    assert parts["recency"] == pytest.approx(1.0, abs=0.01)
    assert 0.0 <= parts["heat"] <= 1.0


def test_heat_ceiling_without_reinforcement_is_exactly_070():
    """0.45*1.0 + 0.25*1.0 + 0.30*0.0 = 0.70, so PROMOTE_HEAT is unreachable."""
    row = {"id": "x", "type": "INFERENCE", "confidence": 1.0, "updated_at": _stamp(0)}
    parts = memory_db.heat_parts(row, [], datetime.now(timezone.utc))
    assert parts["heat"] == pytest.approx(0.70)
    assert parts["heat"] < memory_db.PROMOTE_HEAT


def test_recency_halves_at_thirty_days():
    row = {"id": "x", "type": "FACT", "confidence": 0.0, "updated_at": _stamp(30)}
    parts = memory_db.heat_parts(row, [], datetime.now(timezone.utc))
    assert parts["recency"] == pytest.approx(0.5, abs=1e-6)
    assert parts["age_days"] == pytest.approx(30, abs=0.01)


def test_reinforcement_term_is_saturating():
    row = {"id": "x", "type": "FACT", "confidence": 0.0, "updated_at": _stamp(0)}
    few = memory_db.heat_parts(row, [{"signal": "success", "source": "a"}],
                               datetime.now(timezone.utc))
    assert few["reinforcement"] == pytest.approx(0.25)  # 0.5*0.5 + 0.5*0
    many = memory_db.heat_parts(row, [{"signal": "success", "source": "a"}] * 6,
                                datetime.now(timezone.utc))
    assert many["reinforcement"] == pytest.approx(0.5)


def test_heat_rises_after_success_and_falls_after_decay(conn):
    rid = memory_db.remember(conn, "INFERENCE", "cache warmup runs before traffic")["id"]
    before = memory_db.heat_of(conn, rid)
    memory_db.reinforce(conn, rid, "success", source="runbook")
    after = memory_db.heat_of(conn, rid)
    assert after > before
    memory_db.reinforce(conn, rid, "decay")
    assert memory_db.heat_of(conn, rid) < after


def test_promotion_does_not_fire_on_one_success(conn):
    rid = memory_db.remember(conn, "INFERENCE", "index rebuild needs free disk")["id"]
    memory_db.reinforce(conn, rid, "success", source="runbook")
    assert memory_db.get_record(conn, rid)["type"] == "INFERENCE"
    assert memory_db.stats(conn)["promotions"] == 0


def test_promotion_does_not_fire_on_same_source_twice(conn):
    rid = memory_db.remember(conn, "INFERENCE", "index rebuild needs free disk")["id"]
    memory_db.reinforce(conn, rid, "success", source="Runbook")
    memory_db.reinforce(conn, rid, "success", source=" runbook ")
    assert memory_db.get_record(conn, rid)["type"] == "INFERENCE"
    assert memory_db.stats(conn)["promotions"] == 0


def test_promotion_fires_on_two_distinct_sources(conn):
    rid = memory_db.remember(conn, "INFERENCE", "index rebuild needs free disk")["id"]
    memory_db.reinforce(conn, rid, "success", source="runbook")
    memory_db.reinforce(conn, rid, "success", source="incident-4471")
    rec = memory_db.get_record(conn, rid)
    assert rec["type"] == "FACT"
    assert rec["verified"] == 0, "promotion is not verification"
    assert memory_db.stats(conn)["promotions"] == 1


def test_promotion_log_records_the_evidence(conn):
    rid = memory_db.remember(conn, "ASSUMPTION", "p99 stays under 200ms")["id"]
    memory_db.reinforce(conn, rid, "success", source="loadtest")
    memory_db.reinforce(conn, rid, "success", source="prod-week-12")
    row = conn.execute("SELECT * FROM promotion_log WHERE record_id=?", (rid,)).fetchone()
    assert row["from_type"] == "ASSUMPTION"
    assert row["to_type"] == "FACT"
    assert row["successes"] >= 2
    assert "loadtest" in row["sources"] and "prod-week-12" in row["sources"]
    assert row["heat"] >= memory_db.PROMOTE_HEAT


def test_promotion_only_touches_inference_and_assumption(conn):
    for rtype in ("FACT", "RULE", "CORRECTION", "DECISION"):
        rid = memory_db.remember(conn, rtype, f"statement for {rtype}")["id"]
        memory_db.reinforce(conn, rid, "success", source="a")
        memory_db.reinforce(conn, rid, "success", source="b")
        assert memory_db.get_record(conn, rid)["type"] == rtype


def test_promotion_reported_per_thread(conn):
    rid = memory_db.remember(conn, "INFERENCE", "shards rebalance at 70 percent")["id"]
    memory_db.reinforce(conn, rid, "success", source="a")
    assert memory_db.last_promotion() is None
    memory_db.reinforce(conn, rid, "success", source="b")
    reported = memory_db.last_promotion()
    assert reported and reported["record_id"] == rid


def test_promote_hot_without_candidates_does_not_commit_changes(conn):
    rid = memory_db.remember(conn, "INFERENCE", "quiet statement")["id"]
    result = memory_db.promote_hot(conn, rid)
    assert result is None or result.get("record_id") == rid and result.get("promoted") is False
    assert memory_db.get_record(conn, rid)["type"] == "INFERENCE"


def _cold_record(conn, content, *, rtype="FACT", verified=0, confidence=0.15, days=401):
    rid = memory_db.remember(conn, rtype, content, confidence=confidence,
                             verified=verified)["id"]
    _age(conn, rid, days)
    return rid


def test_forget_requires_every_condition(conn):
    cold = _cold_record(conn, "cold fact with no evidence")
    _cold_record(conn, "verified survivor", verified=1)
    _cold_record(conn, "rule survivor", rtype="RULE")
    _cold_record(conn, "correction survivor", rtype="CORRECTION")
    _cold_record(conn, "confident survivor", confidence=0.9)
    fresh = _cold_record(conn, "fresh survivor", days=5)
    reinforced = _cold_record(conn, "reinforced survivor")
    memory_db.reinforce(conn, reinforced, "decay")
    _age(conn, reinforced, 400)  # old record, and decay is not a survival signal

    protected = _cold_record(conn, "success survivor")
    memory_db.reinforce(conn, protected, "success")
    _age(conn, protected, 400)  # old record, but the success is inside the window

    report = memory_db.forget_cold(conn, apply=True)
    remaining = {r["id"] for r in conn.execute("SELECT id FROM records")}
    assert cold not in remaining
    for keeper in (fresh, protected,
                   memory_db.content_id("verified survivor"),
                   memory_db.content_id("rule survivor"),
                   memory_db.content_id("correction survivor"),
                   memory_db.content_id("confident survivor")):
        assert keeper in remaining, f"{keeper} should have survived"
    assert report["forgotten"] == 2


def test_verified_rule_and_correction_survive_old_low_confidence(conn):
    ids = [
        _cold_record(conn, "verified old", verified=1, confidence=0.05, days=401),
        _cold_record(conn, "rule old", rtype="RULE", confidence=0.05, days=401),
        _cold_record(conn, "correction old", rtype="CORRECTION", confidence=0.05, days=401),
    ]
    memory_db.forget_cold(conn, apply=True)
    remaining = {r["id"] for r in conn.execute("SELECT id FROM records")}
    assert set(ids) <= remaining


def test_apply_false_deletes_nothing(conn):
    rid = _cold_record(conn, "candidate for forgetting")
    report = memory_db.forget_cold(conn, apply=False)
    assert report["forgotten"] == 0
    assert rid in {r["id"] for r in conn.execute("SELECT id FROM records")}
    assert conn.execute("SELECT COUNT(*) FROM forgotten_graveyard").fetchone()[0] == 0


def test_graveyard_holds_a_short_copy_and_fts_is_cleaned(conn):
    content = "obsolete advice " + ("x" * 400)
    rid = _cold_record(conn, content)
    memory_db.forget_cold(conn, apply=True)
    row = conn.execute("SELECT * FROM forgotten_graveyard WHERE id=?", (rid,)).fetchone()
    assert row is not None
    assert len(row["content"]) <= 200
    assert row["type"] == "FACT"
    assert row["reason"]
    assert conn.execute("SELECT COUNT(*) FROM records_fts WHERE id=?", (rid,)).fetchone()[0] == 0
    assert memory_db.search(conn, "obsolete") == []


def test_forget_removes_child_reinforcements(conn):
    rid = _cold_record(conn, "has reinforcement rows")
    conn.execute("INSERT INTO reinforcement (record_id, signal, delta, source, ts)"
                 " VALUES (?, 'success', 0.1, 'old', ?)", (rid, _stamp(800)))
    # The record itself must also be old and at the floor: a live record with a
    # historical signal is not cold, it is merely quiet.
    conn.execute("UPDATE records SET confidence=0.15, updated_at=? WHERE id=?",
                 (_stamp(401), rid))
    conn.commit()
    memory_db.forget_cold(conn, apply=True)
    assert conn.execute("SELECT COUNT(*) FROM reinforcement WHERE record_id=?", (rid,)).fetchone()[0] == 0


def test_decay_floor_constant_is_used_by_apply_decay(conn):
    assert memory_db.DECAY_FLOOR == 0.15
    rid = memory_db.remember(conn, "FACT", "decays toward the floor",
                             confidence=0.9)["id"]
    _age(conn, rid, 4000)
    for _ in range(3):
        memory_db.apply_decay(conn)
    assert memory_db.get_record(conn, rid)["confidence"] == pytest.approx(memory_db.DECAY_FLOOR)


def test_apply_decay_is_idempotent_for_the_same_instant(conn):
    rid = memory_db.remember(conn, "FACT", "decay twice at one instant", confidence=0.9)["id"]
    _age(conn, rid, 30)
    now = datetime.now(timezone.utc)
    first = memory_db.apply_decay(conn, now=now)
    after_first = memory_db.get_record(conn, rid)["confidence"]
    assert first[rid] == pytest.approx(after_first)
    second = memory_db.apply_decay(conn, now=now)
    assert rid not in second, "the same instant must not decay twice"
    assert memory_db.get_record(conn, rid)["confidence"] == pytest.approx(after_first)


def test_migration_v4_is_idempotent(conn, db_path):
    c2 = memory_db.connect(str(db_path))
    try:
        memory_db.init_db(c2)
        tables = {r[0] for r in c2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"forgotten_graveyard", "promotion_log"} <= tables
        assert c2.execute("SELECT version FROM schema_version").fetchone()[0] == memory_db.SCHEMA_VERSION
    finally:
        c2.close()
