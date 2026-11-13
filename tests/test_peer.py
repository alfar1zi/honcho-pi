"""Peer-scoped memory (schema v9: facts can be marked about a peer, and
search filters on that mark so one peer's context never leaks into another's
query."""
from omp_memory import db as memory_db


def test_remember_records_about_peer(conn):
    r = memory_db.remember(conn, "FACT", "prefers rust for tooling",
                           about_peer="ada")
    assert memory_db.get_record(conn, r["id"])["about_peer"] == "ada"


def test_about_peer_defaults_empty(conn):
    r = memory_db.remember(conn, "FACT", "prefers rust for tooling")
    assert memory_db.get_record(conn, r["id"])["about_peer"] == ""


def test_search_filters_by_about_peer(conn):
    memory_db.remember(conn, "FACT", "ada prefers rust for tooling",
                       about_peer="ada")
    memory_db.remember(conn, "FACT", "ben prefers python for tooling",
                       about_peer="ben")
    ada = memory_db.search(conn, "tooling", about_peer="ada")
    assert [r["about_peer"] for r in ada] == ["ada"]
    assert len(memory_db.search(conn, "tooling")) == 2


def test_recall_filters_by_about_peer(conn):
    memory_db.remember(conn, "FACT", "ada ships on thursdays", about_peer="ada",
                       confidence=0.9)
    memory_db.remember(conn, "FACT", "ben ships on fridays", about_peer="ben",
                       confidence=0.9)
    ada = memory_db.recall(conn, "ships", about_peer="ada")
    assert [r["about_peer"] for r in ada] == ["ada"]


def test_peer_filter_does_not_match_empty_default(conn):
    memory_db.remember(conn, "FACT", "plain operator fact")
    assert memory_db.search(conn, "plain operator fact", about_peer="ada") == []
    assert len(memory_db.search(conn, "plain operator fact")) == 1


def test_schema_version(conn):
    assert memory_db.SCHEMA_VERSION == 12
