#!/usr/bin/env python3
"""Generate a demo memory universe for README screenshots.

Deterministic dummy data: a dense core of project knowledge, type clusters
orbiting it, skill families as satellite systems, and cross-links between
regions. Written to a temp DB + graph dir so the live store is untouched.
"""
import json
import math
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
from omp_memory import db  # noqa: E402

random.seed(42)

OUT = Path("/tmp/honcho-demo")
OUT.mkdir(exist_ok=True)
DB = OUT / "memory.db"
if DB.exists():
    DB.unlink()

conn = db.connect(str(DB))
db.init_db(conn)

CORE = [
    ("RULE", "Always run the full gate before pushing: tests, lint, types", 0.95, 1),
    ("FACT", "The store is one SQLite file; WAL mode, FTS5 index, triggers keep it in sync", 0.92, 1),
    ("DECISION", "Stdlib only: no embeddings, no server, no API keys. Boring on purpose", 0.9, 1),
    ("FACT", "Recall ranks by matched-term fraction first, bm25 breaks ties", 0.88, 1),
    ("RULE", "A lesson that fails loses confidence; it never hardens into truth", 0.93, 1),
    ("CORRECTION", "bm25 ranks are negative; normalizing by pool best destroys resolution", 0.85, 1),
    ("FACT", "Content-addressed ids: the same lesson from two sessions merges into one row", 0.9, 1),
    ("DECISION", "Capture commits the DB row first, then appends the JSONL line", 0.87, 1),
    ("RULE", "Never delete a database that fails to open; quarantine and restore", 0.96, 1),
    ("FACT", "Confidence decays with a 30-day half-life toward a 0.15 floor", 0.89, 1),
    ("INFERENCE", "Dense clusters read as regions; orphans belong on an outer ring", 0.7, 0),
    ("FACT", "Verified records and rules are exempt from decay", 0.91, 1),
]

ORBITS = {
    "FACT": [
        "systemd timers need the same env as the live service or writes land in the wrong DB",
        "FTS5 porter stemming makes 'emas' match 'email'; expected, not a bug",
        "WAL siblings must be copied with the DB or the snapshot is half-written",
        "VACUUM INTO produces a consistent snapshot of a live database",
        "The update trigger keeps records_fts in sync on every content edit",
        "Session logs are JSONL; model switches appear as model_change entries",
        "gawk counts characters in UTF-8 locales, mawk counts bytes",
        "A raw copy of a live DB can capture a half-written page",
        "The MCP handshake changed in 2026-07; dual-era servers answer both",
        "GitHub PATs need the workflow scope to push .github/workflows",
        "forceLink mutates the edges array; clone before passing or edges vanish",
        "Port 4173 was taken; the overview lives on 4174",
    ],
    "RULE": [
        "Commit only as the operator; conventional subjects, bodies explain why",
        "Back up the DB plus WAL and SHM siblings before any cleanup",
        "Delete child rows before parents; foreign keys are enforced",
        "Every outbound call gets an explicit timeout, never a library default",
        "E2E runs on a DB copy, never the live store",
        "Test fixtures use dated session filenames, not bare names",
        "Evidence or verified status is required before a rewrite",
        "Images are sniffed by magic bytes, not by extension",
        "The gate runs with PYTHONPATH cleared so the venv is not shadowed",
        "Small surgical diffs; match the style that is already there",
    ],
    "DECISION": [
        "Lexical recall now, embeddings later; the gap is closed by OR fallback",
        "One file beats a service: cp is the backup strategy",
        "Dark canvas with categorical type colors; light theme keeps AA contrast",
        "Threads not wires: thin filaments, labels only where they earn space",
        "The dashboard answers one question: is the store healthy",
    ],
    "CORRECTION": [
        "Typo repair must skip words under 5 chars or it rewrites unrelated queries",
        "The lookbehind for FAILED never matched; skip pytest shapes in the loop instead",
        "Amend preserves dates; set both author and committer dates explicitly",
        "Normalizing lexical scores per pool made every top hit 1.0",
        "The polish commit shipped a crashed web view; stale dist hid it",
        "filter-branch shifts dates by one commit; commit-tree preserves them",
    ],
    "INFERENCE": [
        "Cold records dominate after a capture spike; reinforce before capturing more",
        "Skill families share name fragments; the graph should show them as systems",
        "A label freeze after edits means cosmetics outrank store truth",
        "Hub nodes carry the map; leaves are detail on demand",
        "Most orphans are auto-captured noise; the filter should have caught them",
        "Recall quality tracks corpus hygiene more than ranking tweaks",
    ],
}

SKILLS = {
    "memory": ["memory-recall", "memory-capture", "memory-reflect", "memory-decay", "memory-handoff"],
    "graph": ["graph-layout", "graph-threads", "graph-clusters", "graph-minimap"],
    "gate": ["gate-tests", "gate-lint", "gate-types", "gate-security", "gate-diff"],
    "deploy": ["deploy-venv", "deploy-dist", "deploy-timer", "deploy-backup"],
    "review": ["review-clean-code", "review-design-depth", "review-refactor", "review-legacy"],
}

ids = {}
def add(rtype, content, conf, verified, project):
    rid = db.upsert_record(conn, rtype, content, confidence=conf, verified=verified,
                           source="demo", project_id=project, session_id="demo")
    ids[content] = rid
    return rid

for t, c, conf, v in CORE:
    add(t, c, conf, v, "core")
for t, items in ORBITS.items():
    for c in items:
        add(t, c, round(random.uniform(0.45, 0.9), 2), random.random() < 0.4, t.lower())

# --- layout: core center, type orbits, skill satellite systems ---------------
nodes = []
positions = {}

def place(key, x, y, rtype, label, meta, image=None):
    data = {"label": label, "type": rtype, "meta": meta}
    if image:
        data["image"] = image
    n = {"id": key, "type": "graphNode", "position": {"x": round(x, 1), "y": round(y, 1)},
         "data": data}
    nodes.append(n)
    positions[key] = (x, y)

# core: tight ring around origin
for i, (t, c, conf, v) in enumerate(CORE):
    a = 2 * math.pi * i / len(CORE)
    r = 120 + (i % 3) * 28
    place(f"mem-{ids[c]}", math.cos(a) * r, math.sin(a) * r, t,
          c[:52], f"memory · conf {conf:.2f}" + (" · verified" if v else ""))

# type orbits: each type own ring sector, jittered
TYPE_ANGLE = {"FACT": -90, "RULE": -18, "DECISION": 54, "CORRECTION": 126, "INFERENCE": 198}
for t, items in ORBITS.items():
    base = math.radians(TYPE_ANGLE[t])
    R = 420
    cx, cy = math.cos(base) * R, math.sin(base) * R
    for i, c in enumerate(items):
        a = 2 * math.pi * i / len(items)
        r = 60 + 34 * (i % 3) + random.uniform(-12, 12)
        conf = round(random.uniform(0.45, 0.9), 2)
        place(f"mem-{ids[c]}", cx + math.cos(a) * r, cy + math.sin(a) * r, t,
              c[:52], f"memory · conf {conf:.2f}")

# skill systems: five satellites further out, each a small family cluster
SKILL_ANGLE = [-54, 18, 90, 162, 234]
skill_ids = {}
for si, (fam, members) in enumerate(SKILLS.items()):
    base = math.radians(SKILL_ANGLE[si])
    R = 700
    cx, cy = math.cos(base) * R, math.sin(base) * R
    for i, name in enumerate(members):
        a = 2 * math.pi * i / len(members)
        r = 46 if i else 0
        key = f"skill-managed-{name}"
        skill_ids[name] = key
        place(key, cx + math.cos(a) * r, cy + math.sin(a) * r, "skill",
              name, f"managed skill · {fam} family")

# --- edges: core mesh, orbit-to-core, family rings, cross-links --------------
edges = []
seen = set()
def link(a, b, label="related"):
    key = tuple(sorted((a, b)))
    if key in seen or a == b:
        return
    seen.add(key)
    edges.append({"id": f"e{len(edges)}", "source": a, "target": b, "label": label})

core_keys = [f"mem-{ids[c]}" for _, c, _, _ in CORE]
for i, a in enumerate(core_keys):
    for b in core_keys[i + 1:]:
        if random.random() < 0.5:
            link(a, b, "similar")

for t, items in ORBITS.items():
    keys = [f"mem-{ids[c]}" for c in items]
    for i, k in enumerate(keys):
        link(k, core_keys[i % len(core_keys)])
        if i:
            link(k, keys[i - 1], "similar")
        if random.random() < 0.35:
            link(k, random.choice(keys), "similar")

for fam, members in SKILLS.items():
    keys = [skill_ids[m] for m in members]
    hub = keys[0]
    for k in keys[1:]:
        link(hub, k, "family")
    link(hub, random.choice(core_keys))
    link(hub, random.choice([f"mem-{ids[c]}" for c in ORBITS["RULE"]]))

# cross-orbit bridges make it one universe, not five islands
all_orbit = [f"mem-{ids[c]}" for items in ORBITS.values() for c in items]
for _ in range(24):
    link(random.choice(all_orbit), random.choice(all_orbit), "similar")
for fam in SKILLS:
    a = skill_ids[SKILLS[fam][0]]
    b = skill_ids[random.choice([m for f2, ms in SKILLS.items() if f2 != fam for m in ms])]
    link(a, b, "related")

graph = {"nodes": nodes, "edges": edges}
(OUT / "graph.json").write_text(json.dumps(graph, indent=1))
print(f"demo: {len(nodes)} nodes, {len(edges)} edges -> {OUT}")
conn.close()