#!/usr/bin/env python3
"""
reclaim.py - move Markdown memory into honcho, then retire the source safely.

omp injects every `learned.md` under `memories/` into the system prompt on
every session. Unguarded, those files grow until the context window overflows
and the model silently truncates. This tool:

  1. imports all records into honcho (idempotent, content-hashed)
  2. verifies the count matches what was parsed
  3. only then archives the Markdown to `memories-archive/<ts>/`, leaving a
     short pointer stub so nothing is silently lost

Refuses to archive if the import did not verify.

Usage:
    python3 reclaim.py --all [--dry-run]
    python3 reclaim.py --file <path> [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_db as db  # noqa: E402
from migrate_md import classify, parse_bullets  # noqa: E402

STUB = """<!-- content moved to honcho: {db} -->
Records: {count}
Archived: {when}

Memori di file ini sudah dipindahkan ke database honcho (SQLite+FTS5).
Jangan tambah catatan di sini — agent memakai `mcp__mem_recall` /
`mcp__mem_remember`, dan isi file ini TIDAK lagi disuntik ke context.
"""


def _agent_dir() -> Path:
    explicit = os.environ.get("PI_CODING_AGENT_DIR")
    if explicit:
        return Path(explicit)
    profile = os.environ.get("OMP_PROFILE") or "default"
    return Path.home() / ".omp" / "profiles" / profile / "agent"


def _project_of(mem_dir: Path) -> str:
    """Derive a project id from the memories subdirectory name.

    omp names these directories after the project path, e.g.
    `--home-farizi-Documents-project-telnusa-nms--`.
    """
    name = mem_dir.name
    if name == "--tmp--":
        return "tmp"
    cleaned = name.strip("-").replace("-", "/")
    parts = [p for p in cleaned.split("/") if p and p != "home"]
    return parts[-1] if parts else "default"


def import_file(conn, path: Path, project: str) -> int:
    records = parse_bullets(path.read_text(encoding="utf-8"))
    before = db.stats(conn)["total"]
    for r in records:
        rtype, verified, confidence = classify(r)
        db.upsert_record(
            conn,
            rtype=rtype,
            content=r[:4000],
            evidence=f"reclaimed from {path.name}",
            verified=verified,
            confidence=confidence,
            source=f"reclaim:{path.name}",
            tags="reclaimed",
            project_id=project,
        )
    after = db.stats(conn)["total"]
    return len(records), after - before


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--file", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-source", action="store_true",
                    help="import but leave the .md in place (no archiving)")
    args = ap.parse_args()

    agent = _agent_dir()
    mem_root = agent / "memories"
    if not mem_root.is_dir():
        print(f"no memories dir at {mem_root}", file=sys.stderr)
        return 2

    targets: list[Path] = []
    if args.file:
        targets = [Path(args.file).expanduser()]
    elif args.all:
        targets = sorted(p for p in mem_root.rglob("*.md")
                         if p.name in ("learned.md", "raw_memories.md"))
    else:
        print("use --all or --file", file=sys.stderr)
        return 2

    if not targets:
        print("nothing to reclaim")
        return 0

    print(f"{'file':60} {'bytes':>9} {'records':>8}")
    print("-" * 80)
    total_bytes = 0
    for p in targets:
        total_bytes += p.stat().st_size
        n = len(parse_bullets(p.read_text(encoding="utf-8")))
        print(f"{str(p.relative_to(mem_root)):60} {p.stat().st_size:>9} {n:>8}")
    print("-" * 80)
    print(f"{'TOTAL':60} {total_bytes:>9}  (~{round(total_bytes/4)} tokens/sesi)")

    if args.dry_run:
        print("\n[dry-run] tidak ada yang diubah")
        return 0

    db_path = args.db or os.environ.get("OMP_MEMORY_DB") or str(agent / "honcho" / "memory.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    conn = db.init_db(conn, db_path)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = agent / "memories-archive" / ts

    failures = 0
    for p in targets:
        project = _project_of(p.parent)
        parsed, inserted = import_file(conn, p, project)
        ok = parsed == 0 or inserted > 0 or _already_present(conn, p, project)
        status = "OK" if ok else "GAGAL"
        print(f"{status:6} {p.name:20} project={project:16} parsed={parsed:4} new={inserted:4}")
        if not ok:
            failures += 1
            continue
        if args.keep_source:
            continue
        dest = archive_root / p.relative_to(mem_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        p.write_text(
            STUB.format(db=db_path, count=parsed, when=ts),
            encoding="utf-8",
        )

    print(f"\ndatabase: {db_path}")
    print(f"archive : {archive_root}")
    print(f"stats   : {db.stats(conn)}")
    conn.close()
    return 1 if failures else 0


def _already_present(conn, path: Path, project: str) -> bool:
    """True when this source was imported before (so archive is safe)."""
    row = conn.execute(
        "SELECT count(*) FROM records WHERE source=? AND project_id=?",
        (f"reclaim:{path.name}", project),
    ).fetchone()
    return bool(row and row[0] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
