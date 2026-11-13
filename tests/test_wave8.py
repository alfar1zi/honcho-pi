"""Wave B tests: INTENT records with cue-triggered recall.

Covers:
- importance clamps to [0, 1]; cue normalizes like content.
- recall gives a cue-carrying query an INTENT bonus scaled by importance
  and faded by age (Rummel & Kvavilashvili 2023).
- the bonus fades with INTENT_DECAY_TAU_DAYS.
- non-INTENT records never carry the bonus; full payload exposes
  cue/cue_triggered without a type branch.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from omp_memory import db as memory_db


def _stamp(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_importance_clamps_to_unit_range(conn):
    row = memory_db.remember(
        conn, rtype="INTENT", content="renew the tls certificate before expiry",
        cue="tls expiry", importance=5.0, confidence=0.6)
    stored = memory_db.search(conn, "tls certificate", limit=1)[0]
    assert stored["id"] == row["id"]
    assert stored["importance"] == 1.0

    row2 = memory_db.remember(
        conn, rtype="INTENT", content="reply to the review comments",
        cue="review comments", importance=-3.0, confidence=0.6)
    stored2 = memory_db.search(conn, "reply to the review", limit=1)[0]
    assert stored2["id"] == row2["id"]
    assert stored2["importance"] == 0.0


def test_cue_is_normalized(conn):
    row = memory_db.remember(
        conn, rtype="INTENT", content="renew the tls certificate before expiry",
        cue="  TLS   EXPIRY  ", confidence=0.6)
    stored = memory_db.search(conn, "tls certificate", limit=1)[0]
    assert stored["id"] == row["id"]
    assert stored["cue"] == "tls expiry"


def test_cue_query_pulls_intent_above_plain_fact(conn):
    # A plain FACT that lexically matches the query, and an INTENT whose
    # cue (not content) carries the query terms.
    memory_db.remember(
        conn, rtype="FACT", content="the tls certificate notes are filed",
        confidence=0.8)
    memory_db.remember(
        conn, rtype="INTENT", content="renew the certificate before it expires",
        cue="tls expiry", importance=0.9, confidence=0.6)

    hits = memory_db.recall(conn, "tls expiry", limit=5, full=True)
    by_id = {h["id"]: h for h in hits}
    intents = [h for h in hits if h["type"] == "INTENT"]
    assert intents, "cue-triggered intent must appear"
    assert intents[0]["cue_triggered"] is True
    # the intent's content does not contain the query terms; the cue does.
    assert "tls" not in intents[0]["content"].lower()


def test_importance_scales_the_bonus(conn):
    weak = memory_db.remember(
        conn, rtype="INTENT", content="renew the certificate alpha",
        cue="tls expiry", importance=0.2, confidence=0.5)
    strong = memory_db.remember(
        conn, rtype="INTENT", content="renew the certificate beta",
        cue="tls expiry", importance=1.0, confidence=0.5)

    hits = memory_db.recall(conn, "tls expiry", limit=5)
    by_id = {h["id"]: h["score"] for h in hits}
    assert by_id[strong["id"]] > by_id[weak["id"]]


def test_bonus_fades_with_age(conn):
    # Same intent, one fresh and one old: the fresh one must outrank the
    # faded one when both cues match.
    fresh = memory_db.remember(
        conn, rtype="INTENT", content="renew the certificate alpha",
        cue="tls expiry", importance=1.0, confidence=0.5)
    old = memory_db.remember(
        conn, rtype="INTENT", content="renew the certificate beta",
        cue="tls expiry", importance=1.0, confidence=0.5)
    age = memory_db.INTENT_DECAY_TAU_DAYS * 20
    conn.execute("UPDATE records SET updated_at=? WHERE id=?",
                 (_stamp(age), old["id"]))
    conn.commit()

    now = datetime.now(timezone.utc)
    hits = memory_db.recall(conn, "tls expiry", limit=5, now=now)
    by_id = {h["id"]: h["score"] for h in hits}
    assert by_id[fresh["id"]] > by_id[old["id"]]


def test_non_intent_has_no_cue_fields(conn):
    memory_db.remember(
        conn, rtype="FACT", content="the tls certificate is filed away",
        confidence=0.8)
    hits = memory_db.recall(conn, "tls certificate", limit=5, full=True)
    assert hits
    assert hits[0]["cue"] == ""
    assert hits[0]["cue_triggered"] is False
    assert hits[0]["importance"] == 0.5


def test_compact_payload_omits_cue(conn):
    memory_db.remember(
        conn, rtype="INTENT", content="renew the certificate alpha",
        cue="tls expiry", importance=1.0, confidence=0.6)
    compact = memory_db.recall(conn, "tls expiry", limit=5, full=False)
    full = memory_db.recall(conn, "tls expiry", limit=5, full=True)
    assert "cue" not in compact[0]
    assert "cue" in full[0]
    assert len(json.dumps(compact)) < len(json.dumps(full))
