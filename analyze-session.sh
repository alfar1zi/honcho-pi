#!/usr/bin/env bash
# analyze-session.sh - analyze captured sessions into patterns, reinforce, and promote.
# Reboot-safe: never fails hard, graceful degrade on DB error.
# Hermes-inspired: multi-keyword pattern detection, session lineage analysis, reinforcement learning.

set -uo pipefail

MEMORY_DIR="$HOME/.omp/memory"
DB="$MEMORY_DIR/memory.db"
PYTHON="${PYTHON:-python3}"

MODE="${1:-}"
shift || true

PROJECT=""
SESSION=""
SIGNAL=""
DELTA=0.1
SOURCE="analyze-session"
THRESHOLD=0.85
RECORD_ID=""

cleanup() { :; }
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --session) SESSION="$2"; shift 2 ;;
    --delta)   DELTA="$2"; shift 2 ;;
    --source)  SOURCE="$2"; shift 2 ;;
    --threshold) THRESHOLD="$2"; shift 2 ;;
    *) 
      if [[ -z "$RECORD_ID" ]]; then RECORD_ID="$1"; shift
      elif [[ -z "$SIGNAL" ]]; then SIGNAL="$1"; shift
      else shift; fi
      ;;
  esac
done

"$PYTHON" - "$DB" <<'PYEOF'
import json, sys, os, re
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
from memory_db import connect, init_db, upsert_record, reinforce, get_record, list_records, stats, search

db = sys.argv[1]
mode = sys.argv[2]
project = sys.argv[3] or None
session = sys.argv[3] or ""
signal = sys.argv[3] or ""
record_id = sys.argv[3] or ""
delta = float(sys.argv[3])
source = sys.argv[3]
threshold = float(sys.argv[3])

conn = connect(db)
init_db(conn)

if mode == "stats":
    s = stats(conn)
    print(json.dumps(s, indent=2))
    conn.close()
    sys.exit(0)

if mode == "reinforce":
    if not record_id or not signal:
        print("Usage: analyze-session.sh reinforce <record_id> <signal> [--delta 0.1] [--source ...]")
        sys.exit(2)
    try:
        new_conf = reinforce(conn, record_id, signal, delta, source)
        rec = get_record(conn, record_id)
        print(f"Reinforced {record_id}: signal={signal}, delta={delta}, confidence -> {new_conf}")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    conn.close()
    sys.exit(0)

if mode == "promote":
    records = list_records(conn, project_id=project)
    promoted = 0
    for r in records:
        if r["type"] in ("INFERENCE", "FACT") and r["confidence"] >= threshold and r["verified"] == 1:
            content = r["content"]
            existing = [x for x in list_records(conn, rtype="RULE", project_id=project or None) if x["content"] == content]
            if not existing:
                upsert_record(
                    conn, rtype="RULE", content=content,
                    evidence=r["evidence"], verified=r["verified"], confidence=r["confidence"],
                    source=f"promote:{source}", tags=r["tags"], session_id=r["session_id"], project_id=r["project_id"],
                    parent_id=r["session_id"]
                )
                promoted += 1
                print(f"Promoted {r['id']} -> RULE (conf={new_conf}): {content[:80]}...")
    print(f"Promoted {promoted} records to RULE (threshold={threshold})")
    conn.close()
    sys.exit(0)

if mode == "analyze":
    captures = []
    sessions_dir = os.path.join("$MEMORY_DIR", "sessions")
    if os.path.isdir(sessions_dir):
        for fn in sorted(os.listdir(sessions_dir)):
            if fn.endswith(".jsonl") and not fn.endswith(".env"):
                fpath = os.path.join(sessions_dir, fn)
                with open(fpath) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            captures.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

    db_records = list_records(conn, project_id=project)

    def keywords(text):
        stop = {"the","and","for","with","from","that","this","have","has","had","was","were","been","will","would","could","should","may","might","must","can","just","also","into","onto","over","under","after","before","between","through","during","without","within","about","against","among","upon","toward","towards","across","along","around","behind","below","beneath","beside","besides","by","per","via","vs","via","not","no","yes","ok","okay","okey","okey-dokey","okey-doke"}
        words = re.findall(r'[a-z0-9]{3,}', text.lower())
        return set(w for w in words if w not in stop)

    keyword_groups = defaultdict(list)
    for cap in captures:
        ks = keywords(cap.get("task", "") + " " + cap.get("outcome", ""))
        for k in ks:
            keyword_groups[k].append(cap)

    findings = []
    for kw, items in keyword_groups.items():
        if len(items) >= 2:
            conf = min(0.5 + (len(items) * 0.08), 0.95)
            findings.append({
                "type": "INFERENCE",
                "content": f"Recurring pattern '{kw}': {len(items)} occurrences across sessions",
                "evidence": "; ".join(f"{i.get('task','')} -> {i.get('outcome','')}" for i in items[:5]),
                "confidence": conf,
                "tags": f"pattern,{kw}",
                "source": "analyze-session",
            })

    for cap in captures:
        if cap.get("type") == "CORRECTION":
            findings.append({
                "type": "INFERENCE",
                "content": f"User correction: {cap.get('task','')} -> {cap.get('outcome','')}",
                "evidence": cap.get("evidence", "user correction"),
                "confidence": 0.8,
                "tags": "correction,user-feedback",
                "source": "analyze-session",
            })

    written = 0
    for fnd in findings:
        existing = [x for x in list_records(conn) if x["content"] == fnd["content"] and x["type"] == "INFERENCE"]
        if not existing:
            upsert_record(
                conn, rtype="INFERENCE", content=fnd["content"],
                evidence=fnd["evidence"], verified=0, confidence=fnd["confidence"],
                source=fnd["source"], tags=fnd["tags"], session_id=session or "", project_id=project or ""
            )
            written += 1

    print(f"Analyzed {len(captures)} captures, {len(db_records)} db records, {len(keyword_groups)} keyword groups")
    print(f"Wrote {written} new INFERENCE records")
    conn.close()
    sys.exit(0)

print(f"Unknown mode: {mode}")
sys.exit(2)
