"""Deterministic synthetic corpus for honcho-pi retrieval/dedup measurement.

No I/O, no wall clock, stdlib only. Every query carries exactly one target id
so R@k, hit@1 and MRR have a well-defined answer. Distractors are deliberately
hard: all records sharing an operation share every word except the subject
token, so any retriever that ignores the subject scores ~1/(group size).
"""

import random

TYPE_MIX = (("FACT", 5), ("RULE", 3), ("INFERENCE", 2))

# Operation templates: (name, value generator, paraphrase wording).
# A paraphrase reorders the document's own words plus the subject, so it tests
# word-order and proximity robustness, not unknown vocabulary. Two constraints
# come from the shipped FTS layer and are not stylistic:
# a. No FTS5 stopwords ('for', 'is', 'the'): they are indexed as absent, so an
#    AND over them is unsatisfiable and the query returns zero rows.
# b. The subject is spelled with a space, not the codename hyphen: see build().
OPERATIONS = (
    {
        "name": "retention_policy",
        "value": lambda rng: "%d days" % rng.choice((7, 14, 21, 30, 45, 60, 90)),
        "para": "{value} {subject} retention limit policy",
    },
    {
        "name": "timeout",
        "value": lambda rng: "%d ms" % rng.choice((250, 500, 750, 1000, 1500, 2000, 3000)),
        "para": "{value} deadline {subject} request timeout",
    },
    {
        "name": "batch_size",
        "value": lambda rng: "%d rows" % rng.choice((16, 32, 64, 100, 128, 256, 512)),
        "para": "{subject} write batch size {value}",
    },
    {
        "name": "payout_floor",
        "value": lambda rng: "%d cents" % rng.choice((50, 100, 250, 500, 1000, 2500, 5000)),
        "para": "{value} settlement payout floor {subject}",
    },
)

CANONICAL = "{subject} {op} is {value}"
OP_WORDS = {
    "retention_policy": "retention policy limit",
    "timeout": "request timeout deadline",
    "batch_size": "write batch size",
    "payout_floor": "settlement payout floor",
}

SYLLABLES = (
    "kel", "tor", "vash", "mir", "dun", "sel", "brack", "orv", "thim", "gal",
    "fen", "nur", "pask", "avel", "quor", "zeth", "lorn", "hasp", "iven", "dral",
)


# Paraphrase conventions, both forced by the shipped FTS layer:
# 1. Subject spelled with a space, not the hyphen. db._fts_query quotes each
#    whitespace token while FTS5 indexes the hyphenated codename as two tokens,
#    so a verbatim-hyphenated query matches nothing and would measure a bug, not
#    recall.
# 2. Paraphrase vocabulary stays inside the document's own word set, reordered.
#    FTS5 ANDs every query token, so any novel word zeroes the result; measuring
#    "unknown vocabulary returns nothing" is a separate concern from ranking.

def _codename(rng, used):
    """Two-word codename used as a subject, e.g. 'kestrel-basalt'."""
    while True:
        name = "%s-%s" % (rng.choice(SYLLABLES) + rng.choice(SYLLABLES),
                          rng.choice(SYLLABLES) + rng.choice(SYLLABLES))
        if name not in used:
            used.add(name)
            return name

def _type_sequence(n_records):
    """Types in fixed 5/3/2 proportion; the 10-row cycle repeats exactly."""
    cycle = []
    for rtype, weight in TYPE_MIX:
        cycle.extend([rtype] * weight)
    return [cycle[i % len(cycle)] for i in range(n_records)]


def build(n_records, seed, subject_style="codename"):
    """Return (records, queries) for a synthetic corpus of n_records memories.

    records: list of dicts {id, subject, operation, value, rtype, content, source,
    project_id, verified}. queries: list of {text, target_id, kind} where kind is
    'specific' (canonical wording) or 'paraphrase' (token-disjoint wording).
    queries cover one record per operation group, so each query has one target.
    Distractor design: all records of an operation share OP_WORDS[op] and the
    template, differing only in the subject token.
    """
    if subject_style not in ("codename", "numeric"):
        raise ValueError("subject_style must be 'codename' or 'numeric'")
    rng = random.Random(seed)
    make_subject = _codename if subject_style == "codename" else _numeric
    types = _type_sequence(n_records)
    used = set()
    records = []
    # Round-robin over operations keeps group sizes balanced, so the retrieval
    # chance level is known: 1 / (n_records / len(OPERATIONS)).
    for i in range(n_records):
        op = OPERATIONS[i % len(OPERATIONS)]
        subject = make_subject(rng, used)
        value = op["value"](rng)
        content = CANONICAL.format(subject=subject, op=OP_WORDS[op["name"]], value=value)
        records.append({
            "id": "r%06d" % i,
            "subject": subject,
            "operation": op["name"],
            "value": value,
            "rtype": types[i],
            "content": content,
            "source": "bench",
            "project_id": "proj-a" if i % 2 == 0 else "proj-b",
            "verified": 1 if types[i] == "RULE" else 0,
        })
    queries = []
    for op in OPERATIONS:
        group = [r for r in records if r["operation"] == op["name"]]
        if not group:
            continue
        target = group[rng.randrange(len(group))]
        queries.append({
            "text": target["content"],
            "target_id": target["id"],
            "kind": "specific",
            "operation": op["name"],
        })
        queries.append({
            "text": op["para"].format(subject=target["subject"].replace("-", " "),
                                      value=target["value"]),
            "target_id": target["id"],
            "kind": "paraphrase",
            "operation": op["name"],
        })
    return records, queries


def variants():
    """Six spellings of one fact, as (label, content, source, project_id).

    Measures whether a write path collapses restatements of the same fact: only
    'base' and 'paraphrase' differ in wording; casing, whitespace, source and
    project drift keep the wording intact, which a normalising identity should
    absorb. Source/project currently feed record_id_for, so those two variants
    are expected to fork under the shipped identity scheme.
    """
    fact = "Retention policy for relay backlog is 30 days"
    return [
        ("base", fact, "s1", "p1"),
        ("casing", fact.upper(), "s1", "p1"),
        ("whitespace", "Retention   policy for\nrelay backlog is 30 days", "s1", "p1"),
        ("source", fact, "s2", "p1"),
        ("project", fact, "s1", "p2"),
        ("paraphrase", "relay backlog kept for 30 days", "s1", "p1"),
    ]
