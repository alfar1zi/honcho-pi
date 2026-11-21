"""Wave H: synonym fallback tier.

Concept-group expansion (adopted from Mnemosyne core/synonyms.py, code-audited
2026-11-21) runs ONLY after exact AND, typo repair, and OR relaxation all
return nothing. A query that already matches keeps its ranking untouched.
Synonym hits are discounted so an exact lexical match always outranks them.
"""
from omp_memory import db


def test_synonym_fallback_finds_record_with_disjoint_wording(conn):
    db.remember(conn, "FACT", "the database credential rotates every week",
                evidence="ops runbook", verified=1, source="s1")
    hits = db.recall(conn, "db password rotation", limit=3, full=True)
    assert hits, "synonym tier must bridge db/database and password/credential"
    assert "credential" in hits[0]["content"]


def test_exact_match_outranks_synonym_match(conn):
    db.remember(conn, "FACT", "database credential rotation policy",
                evidence="runbook", verified=1, source="s1")
    db.remember(conn, "FACT", "db password rotation schedule",
                evidence="calendar", verified=1, source="s1")
    hits = db.recall(conn, "database credential", limit=2, full=True)
    assert hits[0]["content"].startswith("database credential")


def test_synonym_tier_does_not_fire_when_exact_matches(conn):
    db.remember(conn, "FACT", "database credential rotation policy",
                evidence="runbook", verified=1, source="s1")
    db.remember(conn, "FACT", "unrelated note about deployment windows",
                evidence="calendar", source="s1")
    hits = db.recall(conn, "database credential", limit=5, full=True)
    assert len(hits) == 1, "synonym expansion must not widen a working query"


def test_unknown_words_stay_inert(conn):
    db.remember(conn, "FACT", "database credential rotation policy",
                evidence="runbook", verified=1, source="s1")
    hits = db.recall(conn, "quixotic zebra furnace", limit=3)
    assert not hits or hits[0].get("abstained")


def test_groups_are_symmetric_and_lowercase():
    for canonical, group in db.SYNONYM_GROUPS.items():
        assert canonical == canonical.lower()
        assert all(w == w.lower() for w in group)
        assert canonical not in group
        for w in group:
            assert db._synonym_group(w), f"{w} must resolve to a group"


def test_synonym_query_builder_quotes_and_groups():
    expr = db._synonym_query("db password")
    assert expr.startswith(">")
    assert '"credential"' in expr and '"database"' in expr
    # every alternative is quoted; grouping parens only, no bare operators
    import re
    bare = re.sub(r'"[^"]*"|\(|\)|OR|AND|\s|>|\*', "", expr)
    assert bare == "", f"unquoted residue: {bare}"
    assert db._synonym_query("quixotic zebra") == ""