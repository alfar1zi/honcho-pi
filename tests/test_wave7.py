"""Wave 7: reconsolidation rewrite gate + supersede links."""
import pytest

from omp_memory import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "wave7.db"))
    db.init_db(c, str(tmp_path / "wave7.db"))
    yield c
    c.close()


def test_success_never_rewrites_content(conn):
    rid = db.remember(conn, "FACT", "deploy host is prod-01", verified=1)["id"]
    db.reinforce(conn, rid, "success",
                 content="deploy host is prod-02", verified=1)
    assert db.get_record(conn, rid)["content"] == "deploy host is prod-01"


def test_correction_without_evidence_refuses_rewrite(conn):
    rid = db.remember(conn, "FACT", "deploy host is prod-01")["id"]
    with pytest.raises(ValueError, match="evidence or verified"):
        db.reinforce(conn, rid, "correction",
                     content="deploy host is prod-02", verified=0)
    assert db.get_record(conn, rid)["content"] == "deploy host is prod-01"


def test_correction_with_evidence_rewrites_and_sets_supersede(conn):
    rid = db.remember(conn, "FACT", "deploy host is prod-01")["id"]
    db.reinforce(conn, rid, "correction",
                 content="deploy host is prod-02",
                 evidence="ops confirmed in #deploys")
    row = db.get_record(conn, rid)
    assert row["content"] == "deploy host is prod-02"
    assert "ops confirmed" in row["evidence"]


def test_superseded_candidate_is_downranked(conn):
    rid = db.remember(conn, "FACT", "deploy host is prod-01")["id"]
    other = db.remember(conn, "FACT", "deploy host is prod-02 in us-east")["id"]
    db.reinforce(conn, rid, "correction",
                 content="deploy host is prod-02",
                 evidence="ops confirmed")
    assert db.get_record(conn, other)["superseded_by"] == rid
    hits = db.recall(conn, "deploy host prod", limit=5)
    ids = [h["id"] for h in hits]
    assert other in ids
    other_score = next(h["score"] for h in hits if h["id"] == other)
    clean = db.remember(conn, "FACT", "deploy host is prod-03 in us-west")["id"]
    db.recall(conn, "deploy host prod", limit=5)
    assert db.get_record(conn, clean)["superseded_by"] == ""


def test_version_bump_is_not_superseded(conn):
    # A different number is a different fact, however similar the words:
    # the stale-side marker must not jump numeric boundaries.
    rid = db.remember(conn, "FACT", "deploy host is prod-01 in us-east")["id"]
    other = db.remember(conn, "FACT", "deploy host is prod-02 in us-east")["id"]
    db.reinforce(conn, rid, "correction",
                 content="deploy host is prod-03 in us-east",
                 evidence="ops confirmed")
    assert db.get_record(conn, other)["superseded_by"] == ""


def test_correction_of_superseded_record_clears_marker(conn):
    # A record that was the stale side, then corrected to the authoritative
    # value, is authoritative again and must not pay the penalty forever.
    rid = db.remember(conn, "FACT", "deploy host is prod-01 in us-east")["id"]
    twin = db.remember(conn, "FACT", "deploy host is prod-01 in us-east")["id"]
    assert twin == rid
    conn.execute("UPDATE records SET superseded_by='x' WHERE id=?", (rid,))
    conn.commit()
    db.reinforce(conn, rid, "correction",
                 content="deploy host is prod-02 in us-east",
                 evidence="ops confirmed")
    assert db.get_record(conn, rid)["superseded_by"] == ""


def test_correction_with_evidence_keeps_verified(conn):
    rid = db.remember(conn, "FACT", "deploy host is prod-01",
                      verified=1)["id"]
    db.reinforce(conn, rid, "correction",
                 content="deploy host is prod-02",
                 evidence="ops confirmed")
    row = db.get_record(conn, rid)
    assert row["verified"] == 1
    assert "ops confirmed" in row["evidence"]


def test_rewrite_window_refreshes_updated_at(conn):
    rid = db.remember(conn, "FACT", "deploy host is prod-01")["id"]
    before = db.get_record(conn, rid)["updated_at"]
    db.reinforce(conn, rid, "correction",
                 content="deploy host is prod-02", verified=1)
    after = db.get_record(conn, rid)["updated_at"]
    assert after >= before


def test_rewrite_rejects_empty_and_oversized_content(conn):
    rid = db.remember(conn, "FACT", "deploy host is prod-01")["id"]
    with pytest.raises(ValueError):
        db.reinforce(conn, rid, "correction", content="   ", verified=1)
    with pytest.raises(ValueError, match="exceeds"):
        db.reinforce(conn, rid, "correction", verified=1,
                     content="x" * (db.REWRITE_MAX_CONTENT_LEN + 1))


def test_superseded_penalty_applies_in_recall(conn):
    rid = db.remember(conn, "FACT",
                      "deploy host is prod-01 in us-east")["id"]
    twin = db.remember(conn, "FACT",
                       "deploy host is prod-02 in us-east")["id"]
    conn.execute("UPDATE records SET superseded_by=? WHERE id=?",
                 (rid, twin))
    conn.commit()
    hits = db.recall(conn, "deploy host prod", limit=5, full=True)
    scores = {h["id"]: h["score"] for h in hits}
    assert scores[rid] > scores[twin]
    # head gets SUPERSEDED_BOOST on top of the loser's SUPERSEDED_PENALTY,
    # so the margin is the sum
    assert scores[rid] - scores[twin] == pytest.approx(
        db.SUPERSEDED_PENALTY + db.SUPERSEDED_BOOST, abs=1e-4)
