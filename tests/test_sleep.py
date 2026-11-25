"""Offline consolidation pass: decay, prune, evict (wake/sleep split).

Lin et al. 2025: maintenance between queries. sleep_pass runs decay,
consolidation, near-duplicate pruning and budget eviction together.
apply=False reports without deleting; apply=True is the operator's decision.
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from omp_memory import db as memory_db


def _old(row_id, conn, days):
    past = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute("UPDATE records SET updated_at=? WHERE id=?", (past, row_id))
    conn.commit()


def test_sleep_pass_merges_near_duplicates(conn):
    # A true duplicate: same words, same (empty) numeric set. The old fixture
    # appended " 2", which is a DIFFERENT fact by the write path's own rule
    # (_upsertable requires identical numeric tokens), so the prune merging it
    # was the bug this test had been locking in.
    a = memory_db.remember(conn, "INFERENCE", "deploy needs free disk space",
                           confidence=0.6)
    b = memory_db.remember(conn, "INFERENCE",
                           "deploy needs some free disk space",
                           confidence=0.4)
    report = memory_db.sleep_pass(conn)
    assert {"keeper": a["id"], "merged": b["id"]} in report["merged"]
    assert memory_db.get_record(conn, b["id"]) is None
    assert memory_db.get_record(conn, a["id"]) is not None
    # audit trail exists
    row = conn.execute(
        "SELECT * FROM forgotten_graveyard WHERE id=?", (b["id"],)).fetchone()
    assert row is not None and "near-duplicate" in row["reason"]


def test_sleep_pass_prune_is_guarded_by_apply(conn):
    # No numeric token on either side: a " 2" suffix is a different fact by
    # the write path's own rule, so the prune must reject it (tested above).
    a = memory_db.remember(conn, "INFERENCE", "cache warmup runs first always")
    b = memory_db.remember(conn, "INFERENCE", "cache warmup runs first")
    report = memory_db.sleep_pass(conn, apply=False)
    assert report["apply"] is False
    assert report["merged"], "report still names the merge"
    assert memory_db.get_record(conn, b["id"]) is not None


def test_sleep_pass_reports_over_budget_irreplaceable_rows(conn):
    # Rate-distortion: prune deletes are recoverable (the keeper holds the
    # fact), so they are the only deletes. A project over budget does not
    # lose its lowest-value rows: they are the last holders of their content
    # and a delete is irreversible, so they stay and are reported.
    keeper = memory_db.remember(conn, "FACT", "rule of thumb for the project",
                                confidence=0.9, project_id="p1")
    weak = memory_db.remember(conn, "INFERENCE", "weak guess number one",
                              confidence=0.2, project_id="p1")
    strong = memory_db.remember(conn, "INFERENCE", "strong guess number two",
                                confidence=0.8, project_id="p1")
    _old(weak["id"], conn, 60)
    report = memory_db.sleep_pass(conn, budget=2)
    assert report["evicted"] == []
    kept = {k["id"] for k in report["kept"]}
    assert weak["id"] in kept
    assert strong["id"] not in kept
    for k in report["kept"]:
        if k["id"] == weak["id"]:
            assert k["reason"] == "over budget, irreplaceable"
    for rid in (keeper["id"], weak["id"], strong["id"]):
        assert memory_db.get_record(conn, rid) is not None


def test_sleep_pass_prune_still_merges_over_budget_duplicates(conn):
    # The prune's own delete (merge into the keeper) survives, including
    # over budget: a merge is recoverable, so it is not an eviction.
    keeper = memory_db.remember(conn, "INFERENCE", "strong guess number two",
                                confidence=0.8, project_id="p1")
    dup = memory_db.remember(conn, "INFERENCE",
                             "strong guess number two strong guess number two",
                             confidence=0.3, project_id="p1", upsert=False)
    _old(dup["id"], conn, 60)
    report = memory_db.sleep_pass(conn, budget=1)
    assert [m["merged"] for m in report["merged"]] == [dup["id"]]
    assert report["evicted"] == []
    assert memory_db.get_record(conn, dup["id"]) is None
    assert memory_db.get_record(conn, keeper["id"]) is not None


def test_sleep_pass_never_evicts_unscoped_or_verified(conn):
    unscoped = memory_db.remember(conn, "INFERENCE", "no project here",
                                  confidence=0.2)
    verified = memory_db.remember(conn, "FACT", "confirmed by operator",
                                  confidence=0.2, project_id="p2", verified=1)
    filler = [memory_db.remember(conn, "INFERENCE", f"filler row {i}",
                                 confidence=0.5, project_id="p2")
              for i in range(3)]
    report = memory_db.sleep_pass(conn, budget=2)
    evicted = {e["id"] for e in report["evicted"]}
    assert unscoped["id"] not in evicted
    assert verified["id"] not in evicted
    assert evicted <= {r["id"] for r in filler}


def test_sleep_pass_reports_decay_and_consolidation(conn):
    rid = memory_db.remember(conn, "INFERENCE", "consolidation target",
                             confidence=0.5)["id"]
    for _ in range(3):
        memory_db.log_retrieval(conn, [rid])
    report = memory_db.sleep_pass(conn)
    assert report["consolidated"], "three retrievals earn the gain"
    rec = memory_db.get_record(conn, rid)
    assert rec["confidence"] == pytest.approx(0.55, abs=1e-6)


def test_sleep_pass_eviction_value_is_confidence_times_recency(conn):
    weak = memory_db.remember(conn, "INFERENCE", "weak and stale",
                              confidence=0.2, project_id="p3")
    _old(weak["id"], conn, 100)
    strong = memory_db.remember(conn, "INFERENCE", "strong and fresh",
                                confidence=0.8, project_id="p3")

    _old(weak["id"], conn, 100)
    report = memory_db.sleep_pass(conn, budget=1)
    assert report["evicted"] == []
    kept = {k["id"]: k for k in report["kept"]}
    assert weak["id"] in kept
    # sleep_pass decays first: 100 days on tau=10, beta=0.5 gives factor 0.3015
    factor = (1 + 100 / memory_db.DECAY_POWER_TAU_DAYS) ** -memory_db.DECAY_POWER_BETA
    conf = memory_db.DECAY_FLOOR + (0.2 - memory_db.DECAY_FLOOR) * factor
    expected = conf * factor
    assert kept[weak["id"]]["value"] == pytest.approx(expected, abs=1e-6)
    assert math.isfinite(kept[weak["id"]]["value"])
