"""
Capture lessons from OMP session logs into honcho memory.

This module is intentionally harness-specific: it reads OMP's session log
format (message/role/user, message/role/toolResult, custom/input/path,
model_change). The logic is copied from handoff.py so the repo stays
independent of any particular harness while the extraction quality matches.

Usage (MCP):
    maintenance {"op": "capture"}
    maintenance {"op": "capture", "apply": true}    # write to DB
    maintenance {"op": "capture", "dry_run": true}  # return candidates without writing

The harness's session log lives in:
    ~/.omp/profiles/<profile>/agent/sessions/<project-slug>/<ts>_<id>.jsonl
OMP_MEMORY_SESSIONS overrides the sessions root.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Re-export for convenience
__all__ = ["extract_lessons", "capture_session", "sessions_dir"]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SESSION_PATTERNS = [
    re.compile(r"\b(error|traceback|failed|exception|exit code [12]|not found|malformed)\b", re.I),
]
_CONTENT_SKIP = re.compile(
    r"^\[(?:HANDOFF|System note)\]|\brecalled memory context\b", re.I
)
_TOKEN_RE = re.compile(r"\w{3,}")
_DEFAULT_SESSIONS = ""  # empty = auto from env

# Keep at module level so callers can mutate before calling extract()
CAPTURE_DEFAULT_CONFIDENCE = 0.35
CAPTURE_DEFAULT_TYPE = "INFERENCE"
MAX_FAILURES_STORED = 8
MAX_FILES_STORED = 30
MAX_FAIL_REPORTED = 8
MAX_FILE_REPORTED = 30


def _agent_dir() -> Path:
    explicit = os.environ.get("PI_CODING_AGENT_DIR")
    if explicit:
        return Path(explicit)
    profile = os.environ.get("OMP_PROFILE") or "default"
    return Path.home() / ".omp" / "profiles" / profile / "agent"


def sessions_dir() -> Path:
    """Root of session logs, or the override via OMP_MEMORY_SESSIONS."""
    override = os.environ.get("OMP_MEMORY_SESSIONS")
    if override:
        return Path(override).expanduser()
    return _agent_dir() / "sessions"


def latest_session() -> Path | None:
    root = sessions_dir()
    if not root.is_dir():
        return None
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _read_entries(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _text_of(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return ""


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def extract_lessons(
    entries: list[dict],
    session_path: Path | None = None,
    max_failures: int = MAX_FAILURES_STORED,
    max_files: int = MAX_FILES_STORED,
) -> list[dict]:
    """Turn a session log into a list of candidate memory records.

    Each candidate is a dict with the keys that db.remember() needs:
        rtype, content, evidence, confidence, source, session_id, project_id, tags.

    The caller decides whether to write them (apply=True) or just inspect them.
    """
    user_turns: list[str] = []
    failures: list[str] = []
    files: "OrderedDict[str, int]" = OrderedDict()

    session_id = session_path.stem if session_path else ""
    project_id = session_path.parent.name if session_path else ""

    for e in entries:
        t = e.get("type")
        if t == "message":
            msg = e.get("message") or {}
            role = msg.get("role")
            if role == "user":
                txt = _text_of(msg).strip()
                if txt and not _CONTENT_SKIP.search(txt):
                    user_turns.append(txt)
            elif role == "toolResult":
                name = str(msg.get("toolName") or "")
                body = _text_of(msg)
                low = body.lower()
                if any(pat.search(low) for pat in _SESSION_PATTERNS):
                    failures.append(f"{name}: {_clip(body, 200)}")
                # track file tool calls
                if name in ("bash", "shell"):
                    for word in _TOKEN_RE.findall(body):
                        if "/" in word and word.startswith("/"):
                            files[word] = files.get(word, 0) + 1
        # Custom entries carry file paths
        if t == "custom":
            payload = e.get("custom") or e.get("data") or {}
            for key in ("path", "file_path", "filePath"):
                p = payload.get(key)
                if isinstance(p, str) and "/" in p:
                    files[p] = files.get(p, 0) + 1
            # Also check top-level input for older entry shapes
            inp = e.get("input") if isinstance(e, dict) else None
            if isinstance(inp, dict):
                for key in ("path", "file_path"):
                    p = inp.get(key)
                    if isinstance(p, str) and "/" in p:
                        files[p] = files.get(p, 0) + 1

    candidates: list[dict] = []

    # Failure patterns → INFERENCE (what went wrong, with confidence floor)
    for failure in failures[:max_failures]:
        # Derive a lesson: "X failed" is evidence about X
        # Strip tool name prefix if it looks like a title
        lesson = failure
        if ":" in failure:
            _, rest = failure.split(":", 1)
            lesson = rest.strip()
        if len(lesson) < 4:
            lesson = failure
        candidates.append({
            "rtype": CAPTURE_DEFAULT_TYPE,
            "content": f"Observed failure: {lesson}",
            "evidence": failure,
            "confidence": CAPTURE_DEFAULT_CONFIDENCE,
            "source": "auto-capture",
            "session_id": session_id,
            "project_id": project_id,
            "tags": "failure,auto-capture",
        })

    # File patterns → ASSUMPTION (which files were worked on)
    if files:
        top_files = list(sorted(files.items(), key=lambda kv: -kv[1]))[:max_files]
        file_list = ", ".join(f"{p}({n}x)" for p, n in top_files)
        candidates.append({
            "rtype": "ASSUMPTION",
            "content": f"Files worked on in this session: {file_list}",
            "evidence": f"{len(files)} unique file(s) touched",
            "confidence": CAPTURE_DEFAULT_CONFIDENCE,
            "source": "auto-capture",
            "session_id": session_id,
            "project_id": project_id,
            "tags": "files,auto-capture",
        })

    # Decision patterns in user turns (heuristic)
    decision_kw = re.compile(
        r"\b(decided?|concluded|chose|agreed|picked|selected|settled on|going with)\b",
        re.I,
    )
    for turn in user_turns:
        m = decision_kw.search(turn)
        if m:
            # extract ~150 chars around the decision keyword
            pos = m.start()
            snippet = turn[max(0, pos - 80):pos + 80]
            candidates.append({
                "rtype": "INFERENCE",
                "content": _clip(f"Possible decision: {snippet}", 300),
                "evidence": f"keyword '{m.group()}' in: {turn[:200]}",
                "confidence": 0.4,
                "source": "auto-capture",
                "session_id": session_id,
                "project_id": project_id,
                "tags": "decision,auto-capture",
            })

    return candidates


def capture_session(
    db_mod,
    conn,
    session_path: Path | None = None,
    apply: bool = False,
    dry_run: bool = False,
) -> dict:
    """Run extraction and optionally write candidates to the DB.

    db_mod: the omp_memory.db module (passed to avoid circular import).
    conn: an open, inited sqlite3.Connection.
    session_path: path to the session .jsonl; default = latest_session().
    apply: write candidates to DB (default False = dry_run mode).
    dry_run: alias for apply=False (explicit).

    Returns a summary dict.
    """
    if session_path is None:
        session_path = latest_session()
    if session_path is None:
        return {"error": "no session file found", "candidates": [], "written": 0}

    entries = _read_entries(session_path)
    if not entries:
        return {"error": "session file empty", "candidates": [], "written": 0}

    candidates = extract_lessons(entries, session_path)

    if not candidates:
        return {
            "session": str(session_path),
            "candidates": [],
            "written": 0,
            "apply": apply,
        }

    if not apply:
        # Return candidates without writing
        readable = [
            {
                "type": c["rtype"],
                "content": c["content"],
                "evidence": c["evidence"],
                "confidence": c["confidence"],
            }
            for c in candidates
        ]
        return {
            "session": str(session_path),
            "candidates": readable,
            "written": 0,
            "apply": False,
        }

    # Write each candidate that doesn't already exist (content-identity dedup)
    written = 0
    for cand in candidates:
        existing = db_mod.find_record(
            conn,
            cand["rtype"],
            cand["content"],
            cand["source"],
            cand["project_id"],
        )
        if existing:
            continue
        db_mod.upsert_record(
            conn,
            rtype=cand["rtype"],
            content=cand["content"],
            evidence=cand["evidence"],
            verified=0,
            confidence=cand["confidence"],
            source=cand["source"],
            tags=cand["tags"],
            session_id=cand["session_id"],
            project_id=cand["project_id"],
        )
        written += 1
    conn.commit()
    return {
        "session": str(session_path),
        "candidates": len(candidates),
        "written": written,
        "apply": True,
    }
