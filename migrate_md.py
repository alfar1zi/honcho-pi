#!/usr/bin/env python3
"""
migrate_md.py - import legacy Markdown memory files into the honcho database.

Legacy layout (omp auto-learn): one bullet per record in `learned.md`, where a
record may wrap across several lines. This importer:

  * splits on top-level bullets (`- ` at column 0) and joins continuations
  * classifies each record into FACT / RULE / CORRECTION / INFERENCE
  * derives confidence from explicit verification words in the text
  * de-duplicates by content hash (the DB enforces this too)

It is idempotent: re-running never duplicates rows.

Usage:
    python3 migrate_md.py <learned.md> [--db PATH] [--project NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_db as db  # noqa: E402


_RULE_MARKERS = (
    "standard", "standar", "rule", "aturan", "must", "harus", "mest",
    "never", "jangan", "always", "selalu", "convention", "konvensi",
)
# A correction is signalled by explicit correction language, not by a stray
# word like "salah" appearing mid-sentence.
_CORRECTION_PATTERNS = (
    r"\bkoreksi\b", r"\bcorrection\b", r"\bternyata\b", r"\bactually\b",
    r"\bmisalkan\b", r"^koreksi", r"\bwrong assumption\b", r"\bsalah kaprah\b",
)
_VERIFIED_MARKERS = (
    "verified", "diverifikasi", "terbukti", "dibuktikan", "confirmed",
    "diuji", "tested", "reproduced",
)
_INFERENCE_MARKERS = (
    "mungkin", "kemungkinan", "perhaps", "likely", "dugaan", "inference",
    "sepertinya", "probably",
)


def classify(text: str) -> tuple[str, int, float]:
    """Return (type, verified, confidence) inferred from record text.

    Priority: CORRECTION (explicit correction language) > RULE (normative
    language) > INFERENCE (hedged language) > FACT.
    """
    low = text.lower()
    verified = 1 if any(m in low for m in _VERIFIED_MARKERS) else 0
    if any(re.search(p, low, re.IGNORECASE) for p in _CORRECTION_PATTERNS):
        rtype = "CORRECTION"
    elif any(m in low for m in _RULE_MARKERS):
        rtype = "RULE"
    elif any(m in low for m in _INFERENCE_MARKERS):
        rtype = "INFERENCE"
    else:
        rtype = "FACT"
    if rtype == "CORRECTION":
        confidence = 0.9 if verified else 0.8
    elif verified:
        confidence = 0.9
    elif rtype == "INFERENCE":
        confidence = 0.55
    elif rtype == "RULE":
        confidence = 0.75
    else:
        confidence = 0.7
    return rtype, verified, confidence


def parse_bullets(text: str) -> list[str]:
    """Split Markdown into records: `- ` starts one, indented lines continue it."""
    records: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if re.match(r"^- ", line):
            if current:
                records.append("\n".join(current).strip())
            current = [line[2:].strip()]
        elif line.startswith(("  ", "\t")) and current:
            current.append(line.strip())
        elif line.strip() and current:
            current.append(line.strip())
        elif current:
            records.append("\n".join(current).strip())
            current = []
    if current:
        records.append("\n".join(current).strip())
    return [r for r in records if len(r) > 15]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--db", default=None)
    ap.add_argument("--project", default="omp")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.source).expanduser()
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 2

    records = parse_bullets(src.read_text(encoding="utf-8"))
    print(f"parsed {len(records)} candidate record(s) from {src}")

    if args.dry_run:
        for r in records[:5]:
            t, v, c = classify(r)
            print(f"  [{t} v={v} c={c}] {r[:90]}")
        return 0

    db_path = args.db or db.DB_PATH
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    conn = db.init_db(conn, db_path)

    before = db.stats(conn)["total"]
    for r in records:
        rtype, verified, confidence = classify(r)
        db.upsert_record(
            conn,
            rtype=rtype,
            content=r[:4000],
            evidence=f"migrated from {src.name}",
            verified=verified,
            confidence=confidence,
            source="migrate:learned.md",
            tags="migrated",
            project_id=args.project,
        )
    after = db.stats(conn)["total"]
    print(f"inserted {after - before} new record(s); total now {after}")
    print(f"database: {db_path}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
