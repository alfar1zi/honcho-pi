"""Near-duplicate detection is advisory only.

No single threshold separates a paraphrase from a near-miss: real paraphrases
scored 0.40 to 0.64 in measurement while adversarial near-misses reached 0.82.
Similarity therefore reports and never merges on its own; auto-merge requires
both a high score and identical numbers.
"""
import pytest

from omp_memory import db as memory_db


def test_similarity_is_jaccard_not_containment():
    a = "alpha beta gamma"
    b = "alpha beta gamma delta epsilon zeta"
    # overlap coefficient would be 1.0 here; Jaccard is 3/6.
    assert memory_db.similarity(a, b) == pytest.approx(0.5)
    assert memory_db.similarity(a, a + " extra words here") < 1.0
    assert memory_db.similarity(a, a) == 1.0


def test_similarity_ignores_punctuation_and_case():
    assert memory_db.similarity("PORT=8080; loopback", "port 8080 loopback") == 1.0


def test_similarity_of_empty_text_is_zero():
    assert memory_db.similarity("", "alpha") == 0.0
    assert memory_db.similarity("  ", "  ") == 0.0


def test_thresholds_have_advisory_gap():
    assert memory_db.SIMILAR_THRESHOLD < memory_db.UPSERT_THRESHOLD


def test_port_case_variant_is_one_row(conn):
    a = memory_db.remember(conn, "FACT", "PORT=8080")
    b = memory_db.remember(conn, "FACT", "port=8080")
    assert a["id"] == b["id"]
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_decide_is_read_only(conn):
    memory_db.remember(conn, "FACT", "the deploy token lives in vault")
    before = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    d = memory_db.decide(conn, "the deploy token lives in vault")
    after = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert before == after
    assert d["action"] == "update"
    assert d["reason"] == "identical"


def test_decide_reports_novel_for_unrelated_content(conn):
    memory_db.remember(conn, "FACT", "sqlite wal mode prevents torn reads")
    d = memory_db.decide(conn, "the release train leaves on thursday")
    assert d["action"] == "create"
    assert d["reason"] == "novel"
    assert d["similar"] == []


def test_decide_reports_similar_without_writing(conn):
    memory_db.remember(conn, "FACT", "payment service timeout is 1000 milliseconds")
    d = memory_db.decide(conn, "payment service timeout equals 1000 milliseconds")
    assert d["action"] == "create"
    assert d["reason"] == "similar"
    assert d["similar"]
    assert d["advisory"]
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_paraphrase_never_auto_merges(conn):
    base = memory_db.remember(conn, "FACT", "Port 8080 must stay bound to loopback in staging")
    par = memory_db.remember(
        conn, "FACT", "Port 8080 must remain bound to loopback in staging", upsert=True
    )
    assert par["id"] != base["id"]
    assert par["applied"] == "inserted"
    assert par["similar"], "a paraphrase must be reported even with upsert on"
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 2


def test_number_drift_never_merges_even_above_threshold(conn):
    a = memory_db.remember(conn, "FACT", "retry budget for section 2 is four attempts")
    b = memory_db.remember(
        conn, "FACT", "retry budget for section 9 is four attempts", upsert=True
    )
    assert a["id"] != b["id"], "different numbers are different facts"
    assert b["applied"] == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 2


def test_adversarial_near_miss_does_not_merge(conn):
    a = memory_db.remember(conn, "FACT", "payment-service 1000")
    b = memory_db.remember(conn, "FACT", "shipping-service 1000", upsert=True)
    assert a["id"] != b["id"]
    assert b["applied"] == "inserted"


def test_advisory_never_changes_row_count(conn):
    memory_db.remember(conn, "FACT", "cache ttl for the feed is sixty seconds")
    count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    for _ in range(3):
        memory_db.decide(conn, "cache ttl for the feed is 60 seconds")
        memory_db.remember(conn, "FACT", "cache ttl for the feed is 30 seconds")
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == count + 1


def test_find_similar_excludes_identical_and_ranks_by_score(conn):
    memory_db.remember(conn, "FACT", "alpha beta gamma delta")
    memory_db.remember(conn, "FACT", "alpha beta gamma epsilon")
    memory_db.remember(conn, "FACT", "alpha beta zeta eta")
    hits = memory_db.find_similar(conn, "alpha beta gamma delta")
    assert hits, "an exact duplicate must not be the only candidate"
    assert all(memory_db.identity_text(h["content"]) != memory_db.identity_text("alpha beta gamma delta")
               for h in hits)
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_find_similar_respects_rtype(conn):
    memory_db.remember(conn, "RULE", "never commit to main on friday")
    hits = memory_db.find_similar(conn, "never commit to main on friday please", rtype="FACT")
    assert hits == []


def test_upsert_merges_provenance_and_keeps_one_row(conn):
    a = memory_db.remember(conn, "FACT", "build cache key includes the lockfile hash",
                           source="s1", project_id="p1")
    b = memory_db.remember(conn, "FACT", "Build cache key includes the lockfile hash",
                           source="s2", project_id="p2")
    assert a["id"] == b["id"]
    assert b["applied"] == "updated"
    rec = memory_db.get_record(conn, a["id"])
    assert rec["content"].startswith("Build"), "richer spelling must win"
    assert "p2" in rec["project_ids"]
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_upsertable_requires_both_guards():
    high_same_number = memory_db._upsertable({"score": 0.9, "content": "section 2 limit 4"}, 
                                            "section 2 limit 4 extra")
    assert high_same_number is True
    low_score = memory_db._upsertable({"score": 0.60, "content": "section 2 limit 4"},
                                      "section 2 limit 4 extra")
    assert low_score is False
    drift = memory_db._upsertable({"score": 0.95, "content": "section 2 limit 4"},
                                  "section 9 limit 4")
    assert drift is False


def test_remember_without_upsert_never_rewrites(conn):
    first = memory_db.remember(conn, "FACT", "payment retries three times")
    second = memory_db.remember(conn, "FACT", "Payment retries three times")
    assert second["id"] == first["id"]
    assert second["applied"] == "updated"
    rows = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert rows == 1


def test_find_similar_returns_score_and_content_together(conn):
    """_upsertable reads both keys; a missing score would fail the guard silently."""
    memory_db.remember(conn, "FACT", "queue depth alarm fires at 500 messages")
    hits = memory_db.find_similar(conn, "queue depth alarm fires at 500 messages now")
    assert hits
    for hit in hits:
        assert "score" in hit and isinstance(hit["score"], float)
        assert hit.get("content")
        assert "same_numbers" in hit


def test_upsert_actually_merges_a_high_score_candidate(conn):
    """The merge path must fire, not just the guard predicates in isolation."""
    first = memory_db.remember(conn, "FACT", "retention policy relay backlog 30 days")
    second = memory_db.remember(
        conn, "FACT", "retention policy for relay backlog 30 days", upsert=True
    )
    assert second["id"] == first["id"]
    assert second["applied"] == "updated"
    assert second["action"] == "update"
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_decide_merge_reason_is_similar(conn):
    """Locked contract: a merge reports reason 'similar', not a new token."""
    first = memory_db.remember(conn, "FACT", "retention policy relay backlog 30 days")
    d = memory_db.decide(conn, "retention policy for relay backlog 30 days", upsert=True)
    assert d["action"] == "update"
    assert d["reason"] == "similar"
    assert d["id"] == first["id"]
    assert d["advisory"]
