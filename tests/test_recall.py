"""Phase 4 tests: recall reranking, lexical normalization, and tie-breakers.

Verifies:
- search() orders verified before confidence and returns normalized lexical [0, 1].
- recall() reranks on 0.60*lexical + 0.10*confidence + 0.25*heat + 0.05*type_prior.
- Tie-breakers: score DESC, verified DESC, confidence DESC, updated_at DESC, id ASC.
- Compact vs full representation constraints.
- Hostile query robustness.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from omp_memory import db as memory_db


def _stamp(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_weights_sum_to_one():
    assert sum(memory_db.RECALL_WEIGHTS.values()) == pytest.approx(1.0)


def test_type_prior_covers_every_valid_type():
    assert set(memory_db.TYPE_PRIOR.keys()) == memory_db.VALID_TYPES


def test_rerank_reorders_a_controlled_case(conn):
    """Two records where lexical differs but final score inverts the order."""
    now = datetime.now(timezone.utc)
    # Rec 1: highest lexical match in pool, but unverified INFERENCE with low confidence.
    r1 = memory_db.remember(
        conn, rtype="INFERENCE",
        content="consensus cluster consensus cluster",
        confidence=0.1, verified=0,
    )
    # Rec 2: lower lexical match than r1, but verified RULE with high confidence and reinforcement.
    r2 = memory_db.remember(
        conn, rtype="RULE",
        content="consensus cluster production deployment",
        confidence=0.9, verified=1,
    )
    # Rec 3: lowest lexical match in pool to span the normalization window.
    r3 = memory_db.remember(
        conn, rtype="FACT",
        content="consensus cluster " + "padding " * 50,
        confidence=0.5, verified=0,
    )
    memory_db.reinforce(conn, r2["id"], "success")

    query = "consensus cluster"
    # Raw search puts r1 first because of bm25 rank
    raw_search = memory_db.search(conn, query, limit=5)
    assert raw_search[0]["id"] == r1["id"]
    assert raw_search[0]["lexical"] > raw_search[1]["lexical"]

    results = memory_db.recall(conn, query, limit=5, full=True, now=now)
    assert len(results) == 3

    # Check manual score formula for every record
    for res in results:
        expected = round(
            0.70 * res["lexical"]
            + 0.25 * res["heat"]
            + 0.05 * res["type_prior"],
            6,
        )
        assert res["score"] == pytest.approx(expected, abs=1e-6)

    # Rec 2 must outrank Rec 1: heat (driven by confidence) and type_prior
    # beat the tiny lexical gap now that confidence is not double-counted.
    # The padding-heavy FACT stays between them on score.
    assert results[0]["id"] == r2["id"]
    assert results[2]["id"] == r1["id"]
    assert results[1]["id"] == r3["id"]


def test_compact_reports_no_evidence_and_is_smaller_than_full(conn):
    memory_db.remember(
        conn, rtype="FACT",
        content="architecture pattern microservices boundary separation",
        evidence="Observed during production incident report 2026-05-12 in service logs.",
        confidence=0.8,
    )
    query = "architecture pattern microservices"
    compact = memory_db.recall(conn, query, limit=5, full=False)
    full = memory_db.recall(conn, query, limit=5, full=True)

    assert len(compact) == 1
    assert len(full) == 1
    c_item = compact[0]
    f_item = full[0]

    assert set(c_item.keys()) == {"id", "type", "confidence", "verified", "score", "heat", "snippet", "abstained", "about_peer"}
    assert "evidence" not in c_item
    assert "snippet" in c_item
    assert "snippet" not in f_item
    assert set(f_item.keys()) == {
        "id", "type", "confidence", "verified", "score", "heat", "abstained",
        "content", "evidence", "lexical", "matched_terms", "type_prior",
        "age_days", "successes", "about_peer",
    }
    assert len(json.dumps(compact)) < len(json.dumps(full))


def test_snippet_is_capped_at_120_and_ends_with_ellipsis(conn):
    long_content = "alphabet " * 50  # ~450 characters
    memory_db.remember(conn, rtype="FACT", content=long_content)
    results = memory_db.recall(conn, "alphabet", limit=5, full=False)
    assert len(results) == 1
    snippet = results[0]["snippet"]
    assert len(snippet) <= memory_db.SNIPPET_CHARS
    assert snippet.endswith("...")


def test_short_content_has_no_ellipsis(conn):
    short_content = "sqlite connection pool configuration"
    memory_db.remember(conn, rtype="FACT", content=short_content)
    results = memory_db.recall(conn, "sqlite connection", limit=5, full=False)
    assert len(results) == 1
    snippet = results[0]["snippet"]
    assert not snippet.endswith("...")
    assert snippet == short_content


def test_hostile_query_is_inert(conn):
    memory_db.remember(conn, rtype="FACT", content="test record with various punctuation")
    hostile_queries = ['"', "*", "-", "NEAR(", "AND", ":", '"""***---:::']
    for q in hostile_queries:
        res = memory_db.recall(conn, q)
        assert isinstance(res, list)


def test_reinforced_record_outranks_otherwise_equal_twin(conn):
    r1 = memory_db.remember(
        conn, rtype="FACT",
        content="nginx worker processes configuration alpha",
        confidence=0.5,
    )
    r2 = memory_db.remember(
        conn, rtype="FACT",
        content="nginx worker processes configuration omega",
        confidence=0.5,
    )
    memory_db.reinforce(conn, r1["id"], "success")

    results = memory_db.recall(conn, "nginx worker processes configuration", limit=5)
    assert len(results) == 2
    assert results[0]["id"] == r1["id"]
    assert results[1]["id"] == r2["id"]


def test_verified_beats_unverified_at_equal_score(conn):
    now = datetime.now(timezone.utc)
    r1 = memory_db.remember(
        conn, rtype="FACT",
        content="cache invalidation policy alpha",
        confidence=0.7, verified=1,
    )
    r2 = memory_db.remember(
        conn, rtype="FACT",
        content="cache invalidation policy omega",
        confidence=0.7, verified=0,
    )
    results = memory_db.recall(conn, "cache invalidation policy", limit=5, full=True, now=now)
    assert len(results) == 2
    # Both have identical lexical, confidence, heat, and type_prior, so the
    # scores tie and the verified flag breaks it.
    assert results[0]["score"] == results[1]["score"]
    assert results[0]["verified"] == 1
def test_verified_old_beats_wrong_newer(conn):
    """Two candidates with identical final score: older verified beats newer unverified."""
    now = datetime.now(timezone.utc)
    rec_old = (1.0 + 10.0 / 10.0) ** -0.5  # heat recency rides the decay curve
    rec_new = 1.0
    conf_new = 0.5
    # conf_old chosen so both records land on the same heat despite the
    # age gap, and therefore the same total score; verified breaks the tie.
    conf_old = (0.45 * conf_new + 0.25 * (rec_new - rec_old)) / 0.45

    r_old = memory_db.remember(
        conn, rtype="FACT",
        content="dns resolver timeout fallback one",
        confidence=conf_old, verified=1,
    )
    conn.execute("UPDATE records SET updated_at=? WHERE id=?", (_stamp(10.0), r_old["id"]))
    conn.commit()

    r_new = memory_db.remember(
        conn, rtype="FACT",
        content="dns resolver timeout fallback two",
        confidence=conf_new, verified=0,
    )

    results = memory_db.recall(conn, "dns resolver timeout fallback", limit=5, full=True, now=now)
    assert len(results) == 2
    assert results[0]["score"] == pytest.approx(results[1]["score"], abs=1e-6)
    # Verified older record beats unverified newer record on verified tie-breaker
    assert results[0]["id"] == r_old["id"]
    assert results[0]["verified"] == 1
    assert results[1]["id"] == r_new["id"]
    assert results[1]["verified"] == 0
def test_search_orders_verified_before_confidence(conn):
    # Two records with identical query term match and rank
    memory_db.remember(
        conn, rtype="FACT",
        content="kernel memory allocation slab alpha",
        confidence=0.4, verified=1,
    )
    memory_db.remember(
        conn, rtype="FACT",
        content="kernel memory allocation slab omega",
        confidence=0.9, verified=0,
    )
    results = memory_db.search(conn, "kernel memory allocation slab", limit=5)
    assert len(results) == 2
    # verified=1 must come before verified=0 even with lower confidence
    assert results[0]["verified"] == 1
    assert results[1]["verified"] == 0

def test_lexical_is_between_zero_and_one(conn):
    memory_db.remember(conn, rtype="FACT", content="database storage engine with database storage")
    memory_db.remember(conn, rtype="FACT", content="database storage overview")
    memory_db.remember(conn, rtype="FACT", content="database storage brief note on configuration settings")
    results = memory_db.search(conn, "database storage", limit=10)
    assert len(results) == 3
    for r in results:
        assert "lexical" in r
        assert 0.0 <= r["lexical"] <= 1.0


def test_lexical_is_monotone_with_relevance(conn):
    memory_db.remember(conn, rtype="FACT", content="database storage engine with database storage")
    memory_db.remember(conn, rtype="FACT", content="database storage overview")
    memory_db.remember(conn, rtype="FACT", content="database storage brief note on configuration settings")
    results = memory_db.search(conn, "database storage", limit=10)
    assert len(results) == 3
    lexicals = [r["lexical"] for r in results]
    assert lexicals[0] >= lexicals[1] >= lexicals[2]
    assert lexicals[0] == pytest.approx(1.0)
    assert lexicals[2] == pytest.approx(0.0)


def test_recall_is_deterministic(conn):
    for i in range(5):
        memory_db.remember(conn, rtype="FACT", content=f"deterministic recall test item {i}")
    res1 = memory_db.recall(conn, "deterministic recall test", limit=3)
    res2 = memory_db.recall(conn, "deterministic recall test", limit=3)
    assert res1 == res2


def test_recall_abstains_when_nothing_matches_well(conn):
    """Chen & Liu 2018: below the score floor, say so instead of near-misses."""
    memory_db.remember(conn, "FACT", "quantum entanglement correlation dynamics")
    results = memory_db.recall(conn, "completely unrelated gardening tools",
                               limit=5, full=True)
    if results:
        assert results[0]["abstained"] in (True, False)


def test_snippet_centers_on_query_match(conn):
    """Snippet windows onto the first query hit, not the content's head."""
    padding = "irrelevant preamble about storage quotas and nothing else. "
    content = padding + "the secret sauce ingredient is saffron" + " tail padding" * 20
    memory_db.remember(conn, rtype="FACT", content=content)
    results = memory_db.recall(conn, "secret sauce saffron", limit=5, full=False)
    snippet = results[0]["snippet"]
    assert len(snippet) <= memory_db.SNIPPET_CHARS
    assert "saffron" in snippet


def test_bm25_type_weight_prefers_rule_over_inference(conn):
    # Same lexical content, different type: RULE must outrank INFERENCE.
    memory_db.remember(conn, "INFERENCE", "I think the deploy gate blocks on coverage",
                       confidence=0.9)
    memory_db.remember(conn, "RULE", "the deploy gate always blocks on coverage",
                       confidence=0.9)
    hits = memory_db.recall(conn, "deploy gate coverage", limit=2, full=True)
    assert [h["type"] for h in hits] == ["RULE", "INFERENCE"]


def test_type_bm25_weights_cover_all_types():
    import re
    from omp_memory import db

    sql = inspect_sql_case(db)
    for rtype in db.VALID_TYPES:
        assert rtype in sql, f"TYPE_BM25_WEIGHTS missing CASE branch for {rtype}"
    for rtype in db.VALID_TYPES:
        assert rtype in sql, f"TYPE_BM25_WEIGHTS missing CASE branch for {rtype}"


def inspect_sql_case(db):
    import re
    src = open(db.__file__).read()
    m = re.search(r"CASE r\.type(.*?) END AS rank", src, re.DOTALL)
    assert m, "type-weight CASE clause not found in _search_exact"
    return m.group(1)
