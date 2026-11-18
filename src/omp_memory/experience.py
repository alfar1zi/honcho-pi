"""Experience layer: cross-trajectory abstraction & pattern detection.

Covers the survey's Storage->Reflection->Experience arc: capture already
stores trajectories (Wave F); reflect evaluates them into PROCEDURAL,
REFLECTIVE and RULE knowledge, pairs each stored failure with its successful
counterpart (ReasoningBank, arXiv 2509.25140), and links episodes that
describe the same recurring situation across sessions and projects.
"""
import math
import re
import sqlite3

from omp_memory import db

# A topic cluster is a group of records whose TF-IDF vectors clear this
# cosine. idf is log(1 + n/df): a token shared by every document in the
# pool (df == n) keeps positive weight, because shared topic vocabulary is
# exactly what should pull records together. The threshold sits below
# web.py's record-record edge threshold on purpose: a cluster has to
# survive paraphrase, an edge only has to suggest.
_TOPIC_SIMILARITY = 0.15
# A pattern needs repeated evidence, not a single coincidence.
_MIN_GROUP = 3
_MIN_PROJECTS = 2
_MIN_REINFORCE = 2
_EXPERIENCE_SOURCE = "experience"


def _tokens(text: str) -> list:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 4]


def _tfidf(docs: list) -> list:
    n = len(docs)
    df: dict = {}
    tokenized = [_tokens(doc) for doc in docs]
    for toks in tokenized:
        for w in set(toks):
            df[w] = df.get(w, 0) + 1
    out = []
    for toks in tokenized:
        tf: dict = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        out.append({w: (1 + math.log(c)) * math.log(1 + n / df[w])
                    for w, c in tf.items()})
    return out


def _cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[w] * b[w] for w in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _top_terms(vec: dict, k: int = 6) -> str:
    return ", ".join(w for w, _ in
                     sorted(vec.items(), key=lambda kv: -kv[1])[:k])


def _topic_clusters(conn: sqlite3.Connection) -> list:
    """Greedy single-pass clustering of every record by topic.

    A record joins the first cluster whose representative vector it
    resembles, otherwise it opens a new cluster. Returns clusters in
    discovery order, each with its representative vec and member rows.
    """
    rows = conn.execute(
        "SELECT id, type, content, project_id, session_id, tags, source FROM records"
        " ORDER BY created_at").fetchall()
    if len(rows) < _MIN_GROUP:
        return []
    vecs = _tfidf([r["content"] for r in rows])
    clusters = []
    for row, vec in zip(rows, vecs):
        target = None
        for c in clusters:
            if _cosine(vec, c["vec"]) >= _TOPIC_SIMILARITY:
                target = c
                break
        if target is None:
            target = {"vec": vec, "rows": []}
            clusters.append(target)
        target["rows"].append(row)
    return clusters


def _reinforce_counts(conn: sqlite3.Connection) -> dict:
    try:
        rows = conn.execute(
            "SELECT record_id, signal FROM reinforcement").fetchall()
    except sqlite3.OperationalError:
        return {}
    counts: dict = {}
    for r in rows:
        if r["signal"] == "success":
            counts[r["record_id"]] = counts.get(r["record_id"], 0) + 1
    return counts


def _existing_pattern_event_ids(conn: sqlite3.Connection) -> set:
    rows = conn.execute(
        "SELECT event_id FROM records WHERE source=?", (_EXPERIENCE_SOURCE,)
    ).fetchall()
    return {r["event_id"] for r in rows if r["event_id"]}


def detect_patterns(conn: sqlite3.Connection) -> list:
    """Scan the whole store for cross-trajectory knowledge candidates.

    Returns candidate dicts (not written): one per detected pattern, each
    with rtype, content, evidence, confidence, tags, member_ids and a
    deterministic event_id so re-running reflect is idempotent via
    upsert_record. Failure-majority clusters are flagged by pattern_flags
    instead: they mark a recurring mistake, not a working pattern.
    """
    reinforces = _reinforce_counts(conn)
    out = []
    for cluster in _topic_clusters(conn):
        members = cluster["rows"]
        projects = {r["project_id"] for r in members if r["project_id"]}
        if len(members) < _MIN_GROUP or len(projects) < _MIN_PROJECTS:
            continue
        failures = [r for r in members if "failure" in (r["tags"] or "")]
        if len(failures) * 2 > len(members):
            continue
        types = {r["type"] for r in members}
        top = _top_terms(cluster["vec"])
        # event_id from non-experience members only: a written pattern
        # joins its own cluster on the next pass, which would otherwise
        # change the id and break idempotency.
        ids = sorted(r["id"] for r in members
                     if r["source"] != _EXPERIENCE_SOURCE) or \
            sorted(r["id"] for r in members)
        eid = "exp:" + db.sha(",".join(ids))[:16]

        # ReasoningBank contrastive induction: corrections next to facts
        # on the same topic teach what actually works.
        if "CORRECTION" in types and ("FACT" in types or "RULE" in types):
            corr = next(r for r in members if r["type"] == "CORRECTION")
            content = (f"Correction pattern: {corr['content']} "
                       f"applies to topic '{top}'")
            out.append({"rtype": "REFLECTIVE", "content": content,
                        "evidence": f"{len(members)} records on '{top}' "
                                    f"across {len(projects)} projects",
                        "confidence": 0.5, "source": _EXPERIENCE_SOURCE,
                        "tags": "pattern,experience", "event_id": eid,
                        "member_ids": ids})
            continue

        success = sum(reinforces.get(rid, 0) for rid in ids)
        if success >= _MIN_REINFORCE or "RULE" in types:
            content = (f"Working pattern for '{top}': "
                       f"{_top_terms(cluster['vec'], 8)}")
            out.append({"rtype": "PROCEDURAL", "content": content,
                        "evidence": f"{len(members)} records on '{top}' "
                                    f"across {len(projects)} projects, "
                                    f"{success} reinforcement successes",
                        "confidence": 0.55, "source": _EXPERIENCE_SOURCE,
                        "tags": "pattern,experience", "event_id": eid,
                        "member_ids": ids})
    return out


def pattern_flags(conn: sqlite3.Connection) -> list:
    """Flag recurring situations: repeated failures, repeated rules.

    Flags are observations for the operator, not records to write: a
    recurring failure says something is still broken somewhere.
    """
    flags = []
    for cluster in _topic_clusters(conn):
        members = cluster["rows"]
        projects = {r["project_id"] for r in members if r["project_id"]}
        if len(members) < _MIN_GROUP or len(projects) < _MIN_PROJECTS:
            continue
        top = _top_terms(cluster["vec"])
        failures = [r for r in members if "failure" in (r["tags"] or "")]
        failure_projects = {r["project_id"] for r in failures
                            if r["project_id"]}
        if (len(failures) >= _MIN_GROUP
                and len(failure_projects) >= _MIN_PROJECTS):
            flags.append({"kind": "recurring_failure", "topic": top,
                          "members": len(failures),
                          "projects": len(failure_projects)})
        if "RULE" in {r["type"] for r in members}:
            flags.append({"kind": "recurring_rule", "topic": top,
                          "members": len(members),
                          "projects": len(projects)})
    return flags


def contrastive_pairs(conn: sqlite3.Connection) -> list:
    """Pair each stored failure with the verified success on its topic.

    A failure record only pairs when it carries the failure tag (captured
    failures, not every record mentioning an error word). Success must be
    verified: an unverified guess is not a lesson, it is a second guess.
    """
    failures = conn.execute(
        "SELECT id, content FROM records WHERE tags LIKE '%failure%'"
        " ORDER BY created_at").fetchall()
    if not failures:
        return []
    successes = conn.execute(
        "SELECT id, content, type FROM records"
        " WHERE verified=1 AND type IN ('FACT','RULE')"
        " ORDER BY created_at").fetchall()
    if not successes:
        return []
    pairs = []
    for f in failures:
        best, best_sim = None, 0.0
        for s in successes:
            sim = db.similarity(f["content"], s["content"])
            if sim > best_sim:
                best, best_sim = s, sim
        if best is not None and best_sim >= _TOPIC_SIMILARITY:
            pairs.append({"failure_id": f["id"], "success_id": best["id"],
                          "similarity": round(best_sim, 4),
                          "failure": f["content"], "success": best["content"]})
    return pairs


def event_links(conn: sqlite3.Connection) -> list:
    """Link event episodes across sessions that describe the same situation.

    Events are grouped by the event_id capture wrote (session#eN). Two
    events link when their pooled contents are similar enough; the link
    itself is symmetric and deduplicated by construction (each pair is
    visited once from the lower index).
    """
    rows = conn.execute(
        "SELECT event_id, content FROM records"
        " WHERE event_id != '' ORDER BY created_at").fetchall()
    if not rows:
        return []
    by_event: dict = {}
    order: list = []
    for r in rows:
        if r["event_id"] not in by_event:
            by_event[r["event_id"]] = []
            order.append(r["event_id"])
        by_event[r["event_id"]].append(r["content"])
    if len(order) < 2:
        return []
    vecs = _tfidf([" ".join(by_event[e]) for e in order])
    links = []
    for i in range(len(order)):
        for j in range(i + 1, len(order)):
            sim = _cosine(vecs[i], vecs[j])
            if sim >= _TOPIC_SIMILARITY:
                links.append({"a": order[i], "b": order[j],
                              "similarity": round(sim, 4)})
    return links


def reflect(conn: sqlite3.Connection, apply: bool = False) -> dict:
    """Run the full experience pass: patterns, flags, pairs, links.

    Returns a summary. With apply=True the candidates are written through
    db.upsert_record, whose content-identity dedup plus the deterministic
    event_id makes re-running reflect a no-op on an unchanged store.
    Candidates whose event_id is already in the store are neither counted
    nor written, so the summary describes what this pass changed.
    """
    candidates = detect_patterns(conn)
    flags = pattern_flags(conn)
    pairs = contrastive_pairs(conn)
    links = event_links(conn)
    have = _existing_pattern_event_ids(conn)
    fresh = [c for c in candidates if c["event_id"] not in have]
    written = 0
    reflective = sum(1 for c in fresh if c["rtype"] == "REFLECTIVE")
    if apply:
        for c in fresh:
            db.upsert_record(conn, c["rtype"], c["content"],
                             evidence=c["evidence"], verified=0,
                             confidence=c["confidence"], source=c["source"],
                             tags=c["tags"], event_id=c["event_id"],
                             parent_id=",".join(c["member_ids"]))
            have.add(c["event_id"])
            written += 1
        for p in pairs:
            content = (f"Lesson from failure: {p['failure']} "
                       f"-> worked instead: {p['success']}")
            rid = db.upsert_record(conn, "REFLECTIVE", content,
                                   evidence=f"paired records "
                                            f"{p['failure_id']} and "
                                            f"{p['success_id']}",
                                   verified=0, confidence=0.5,
                                   source=_EXPERIENCE_SOURCE,
                                   tags="reflective,experience",
                                   parent_id=f"{p['failure_id']},"
                                             f"{p['success_id']}")
            if rid:
                written += 1
                reflective += 1
    return {"procedural": sum(1 for c in fresh
                              if c["rtype"] == "PROCEDURAL"),
            "reflective": reflective,
            "pairs": len(pairs), "links": len(links),
            "patterns": flags, "written": written}
