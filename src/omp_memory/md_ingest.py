"""Ingest omp's builtin markdown memory into the honcho store.

Why this exists: omp's `learn` tool writes appended entries into
`memories/<project>/learned.md`, while the top of that same file claims memory
moved to honcho and tells the agent not to add notes there. Measured on
2026-11-26: `memories/--tmp--/learned.md` held 80 entries appended *after*
that tombstone, and all 80 were absent from the store - because the migration
that copied these files ran once (newest imported record 2026-11-18) and
nothing has read them since.

This module closes the fork without moving the files: it reads them
incrementally, keyed by (path, mtime, size, sha256), so a re-run is cheap and
an edited file is re-read while an untouched one costs one stat.

Design rules, each from a measured failure:
  * Skip the tombstone block itself - it is boilerplate, not a lesson. Only
    entries after the tombstone's end marker are candidates.
  * An entry shorter than MIN_ENTRY_CHARS is a heading or a pointer, not a
    fact worth a row.
  * Content identity does the de-duplication: `upsert_record` is
    content-addressed, so ingesting the same line twice is a no-op rather
    than a duplicate row.
  * A source tag records the file, so an operator can trace a record back.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

MIN_ENTRY_CHARS = 60
STATE_NAME = "ingest_md.json"
# Which filenames are curated lessons. `raw_memories.md` is the full narrative
# of each rollout (1,605 candidate entries across 170 files, dominated by
# multi-paragraph session prose); ingesting it would bury the store in
# narrative that recall then has to rank against real facts. `rollout_summaries`
# is the same material again. Curated lesson files only.
INGEST_FILENAMES = ("learned.md", "MEMORY.md")

# The tombstone block ends at the first line that is not part of it. These are
# the markers actually present in the files on this machine.
_TOMBSTONE_MARKERS = (
    "content moved to honcho",
    "sudah dipindahkan ke database honcho",
    "jangan tambah catatan di sini",
)

_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")


def memories_root() -> Path:
    """Directory holding omp's per-project memory folders."""
    explicit = os.environ.get("OMP_MEMORY_MD_ROOT")
    if explicit:
        return Path(explicit).expanduser()
    agent = os.environ.get("PI_CODING_AGENT_DIR")
    if agent:
        return Path(agent) / "memories"
    profile = os.environ.get("OMP_PROFILE") or "default"
    return Path.home() / ".omp" / "profiles" / profile / "agent" / "memories"


def _state_path(db_dir: Path) -> Path:
    return db_dir / STATE_NAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signature(path: Path) -> dict:
    stat = path.stat()
    return {"mtime": int(stat.st_mtime), "size": stat.st_size, "sha256": _sha256(path)}


def entries_after_tombstone(text: str) -> list[str]:
    """Bullet entries that follow the 'moved to honcho' notice.

    The notice is real and must be skipped; what follows it is what `learn`
    appended afterwards, which is exactly the part honcho never saw.
    """
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if any(marker in line.lower() for marker in _TOMBSTONE_MARKERS):
            start = i + 1
            break
    out = []
    for line in lines[start:]:
        match = _BULLET_RE.match(line.strip())
        if not match:
            continue
        body = match.group(1).strip()
        if len(body) >= MIN_ENTRY_CHARS:
            out.append(body)
    return out


def ingest_md(conn, db_mod, root: Path = None, apply: bool = False) -> dict:
    """Read every memories/**/*.md file and upsert new entries.

    db_mod is the omp_memory.db module (passed to avoid a circular import).
    Returns counts plus the per-file verdicts, so a caller can see which files
    were skipped and why.
    """
    root = Path(root) if root is not None else memories_root()
    if not root.is_dir():
        return {"root": str(root), "error": "memories directory not found",
                "files": 0, "written": 0}
    db_dir = Path(db_mod.DB_PATH).parent
    state_path = _state_path(db_dir)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}

    files = sorted(p for p in root.rglob("*.md") if p.name in INGEST_FILENAMES)
    written = 0
    considered = 0
    details = []
    for path in files:
        key = str(path)
        try:
            sig = _signature(path)
        except OSError as exc:
            details.append({"file": key, "skipped": f"unreadable: {exc}"})
            continue
        if state.get(key) == sig:
            details.append({"file": key, "skipped": "unchanged"})
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            details.append({"file": key, "skipped": f"unreadable: {exc}"})
            continue
        entries = entries_after_tombstone(text)
        considered += len(entries)
        # Order matters: the folder is "-Documents-project-demo--", so stripping
        # hyphens first turns the marker into "Documents-project-demo" and the
        # project name becomes the whole path. Replace the marker first, then
        # trim the leftover separator hyphens.
        folder = path.parent.name
        project = folder.replace("-Documents-project-", "").replace(
            "Documents-project-", "").strip("-")
        if apply:
            for body in entries:
                db_mod.remember(
                    conn,
                    rtype="FACT",
                    content=body,
                    evidence=f"omp builtin memory: {path.name}",
                    confidence=0.55,
                    source=f"md:{path.parent.name}"[:120],
                    project_id=project,
                )
                written += 1
            state[key] = sig
        details.append({"file": key, "entries": len(entries), "project": project})

    if apply:
        db_dir.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        conn.commit()
    return {
        "root": str(root),
        "files": len(files),
        "entries": considered,
        "written": written,
        "apply": bool(apply),
        "details": details,
    }
