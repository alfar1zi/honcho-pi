"""Wave K: graphify audit adoptions.

- Vocabulary gate: a query whose words exist nowhere in the index abstains
  instead of answering from one incidental shared token.
- Hub penalty in PPR: mass flowing through a high-degree non-seed node is
  discounted, so association is not an accident of degree.
- Weakest-hop confidence: an associative hit's bonus is bounded by the
  strength of its link, mirroring graphify's confidence = weakest hop.
"""
from omp_memory import db


def test_vocabulary_gate_abstains_on_unknown_words(conn):
    db.remember(conn, "FACT", "wal mode fixes the database lock timeout",
                evidence="e", verified=1, source="s1")
    hits = db.recall(conn, "quixotic zebra furnace", limit=3, full=True)
    assert not hits or hits[0]["abstained"]


def test_vocabulary_gate_keeps_partial_known_queries(conn):
    db.remember(conn, "FACT", "wal mode fixes the database lock timeout",
                evidence="e", verified=1, source="s1")
    hits = db.recall(conn, "wal quixotic", limit=3, full=True)
    assert hits and not hits[0]["abstained"]


def test_synonym_still_works_when_literal_words_unknown(conn):
    db.remember(conn, "FACT", "the database credential rotates every week",
                evidence="e", verified=1, source="s1")
    hits = db.recall(conn, "db password", limit=3, full=True)
    assert hits and "credential" in hits[0]["content"]


def test_hub_node_does_not_dominate_ppr(conn):
    # "gateway" is a 12-member hub; "retry budget" is a 2-member link.
    # Mass must follow the rare link, not the hub.
    seed = db.remember(conn, "FACT", "gateway retry budget is three attempts",
                       evidence="e", verified=1, source="s1")
    for i in range(10):
        db.remember(conn, "FACT", f"gateway node {i} runs in region {i}",
                    evidence="e", source="s1")
    rare = db.remember(conn, "FACT", "retry budget escalation path is pagerduty",
                       evidence="e", source="s1")
    rows = db.search(conn, "gateway escalation pagerduty", limit=20, pool=20)
    assert any(r["id"] == rare["id"] for r in rows), "pool must reach the rare record"
    ppr = db._ppr_expand(conn, [r for r in rows if r["id"] == seed["id"]], rows)
    hub_ids = [r["id"] for r in rows if "runs in region" in r["content"]]
    hub_mass = max((ppr.get(i, 0.0) for i in hub_ids), default=0.0)
    assert ppr.get(rare["id"], 0.0) > hub_mass


def test_bonus_follows_link_strength(conn):
    # A record joined by a rare term must out-earn one joined only by the
    # 31-member hub word "deploy" (graphify weakest-hop discipline).
    for i in range(30):
        db.remember(conn, "FACT", f"deploy step {i} touches service {i}",
                    evidence="e", source="s1")
    seed = db.remember(conn, "FACT", "deploy checklist starts with a backup",
                       evidence="e", source="s1")
    common = db.remember(conn, "FACT", "deploy window opens on tuesday",
                         evidence="e", source="s1")
    rare = db.remember(conn, "FACT", "checklist review happens before merge",
                       evidence="e", source="s1")
    rows = db.search(conn, "deploy checklist review", limit=20, pool=20)
    ids = {r["id"] for r in rows}
    assert {seed["id"], common["id"], rare["id"]} <= ids
    bonus = db._ppr_bonus(conn, [r for r in rows if r["id"] == seed["id"]], rows)
    assert bonus.get(rare["id"], 0.0) > bonus.get(common["id"], 0.0)
    assert all(v <= db.PPR_WEIGHT for v in bonus.values())