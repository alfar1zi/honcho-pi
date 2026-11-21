"""
Capture lessons from OMP session logs into honcho memory.

This module is intentionally harness-specific: it reads OMP's session log
format (message/role/user, message/role/toolResult, custom/input/path,
model_change). The logic is copied from handoff.py so the repo stays
independent of any particular harness while the extraction quality matches.

Usage (MCP):
    maintenance {"op": "capture"}
    maintenance {"op": "capture", "apply": true}    # write to DB
    maintenance {"op": "capture", "reflect": true}  # + reflect pass

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

_FAILURE_RE = re.compile(
    r"(exited with code [1-9]"
    r"|exit code [1-9]"
    r"|traceback \(most recent call last\)"
    r"|\b\w+(?:Error|Exception)\b\s*:"
    r"|\bHTTP (?:Error \d{3}|\d{3})"
    r"|\b\d{3} (?:Not Found|Internal Server Error|Bad Gateway|Service Unavailable)"
    r"|\"failed\":\s*[1-9]"
    r"|\b[1-9][0-9]* failed\b"
    r"|\berror:)"
    , re.I
)
# A failure line is a lesson only when it names the error type. Bare
# "Command exited with code N", parse-error fragments, and echoed error
# text from a script under test are noise, not knowledge.
_LESSON_RE = re.compile(
    r"(?i:(?<!except )(?<!raise )\b(?!isError\b)(?!STATUS_)\w+(?:Error|Exception)\b\s*:)"
    r"|\bFAILED\s+\S+"
    r"|\bE\s+[A-Z]\w+"
    r"|\bTraceback"
)
# `FAILED tests/...` is pytest output: the agent running its own suite.
# The fix lands in the commit, not in memory, so candidates matching this
# shape are dropped in the failure loop. Assertion errors from other
# tooling (no tests/ path) still carry a real lesson.
_PYTEST_RE = re.compile(r"\bFAILED\s+(?:\./)?(?:tests?|test)/\S+")
# Lines that merely name an exception (except clauses, raise statements)
# are source code under test, not an outcome.
_NOISE_RE = re.compile(r"(?:^|\b\w+:\s)\s*(?:except|raise)\s+\S*?(?:Error|Exception)\b", re.I)
# Tools whose output is content, not an outcome: reading a file that mentions
# an error is not a failure. Only execution tools can fail.
_READ_TOOLS = {"read", "grep", "glob", "view", "search"}
_CONTENT_SKIP = re.compile(
    r"^\[(?:HANDOFF|System note)\]|\brecalled memory context\b"
    r"|\bObserved failure:", re.I
)
_TOKEN_RE = re.compile(r"\w{3,}")
_DEFAULT_SESSIONS = ""  # empty = auto from env

# Keep at module level so callers can mutate before calling extract()
CAPTURE_DEFAULT_CONFIDENCE = 0.35
# Auto-captured lessons are pattern-matched from a transcript, not
# verified by anyone: clamp them well below the trust bar so a noisy
# session cannot outrank a confirmed fact.
CAPTURE_CONFIDENCE_CEILING = 0.6
CAPTURE_DEFAULT_TYPE = "INFERENCE"
MAX_FAILURES_STORED = 8
MAX_FILES_STORED = 30
MAX_FAIL_REPORTED = 8
MAX_FILE_REPORTED = 30

# Event segmentation (Wave F, EM-LLM 2407.9450): a session log is not one
# memory episode. Split on time gaps or topic shifts so a failure in a
# midnight deploy session does not inherit the tags of the refactor that
# followed at breakfast.
EVENT_GAP_MINUTES = 30.0
EVENT_TOPIC_OVERLAP = 0.2
EVENT_TAG_PREFIX = "event:"


# Secret-shape patterns, shared by the CLI capture guard and the auto-capture
# pipeline. Patterns run in Python so the payload never reaches a shell.
SECRET_PATTERNS = [
    ("api_key", r"sk-[A-Za-z0-9_\-]{8,}"),
    ("api_key", r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{8,}"),
    ("github_token", r"ghp_[A-Za-z0-9]{20,}"),
    ("aws_key", r"AKIA[0-9A-Z]{16}"),
    ("bearer", r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
    ("private_key", r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    ("assignment", r"(?i)(?:password|passwd|secret|api_key|apikey|token|credential)\s*[=:]\s*\S{6,}"),
]


def scan_text(text: str) -> list:
    """Return [(kind, matched_fragment)] for secret-looking input."""
    findings = []
    for kind, pattern in SECRET_PATTERNS:
        match = re.search(pattern, text or "")
        if match:
            findings.append((kind, match.group(0)[:24]))
    return findings


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


_SESSION_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T.*\.jsonl$")


def latest_session() -> Path | None:
    root = sessions_dir()
    if not root.is_dir():
        return None
    files = [
        p for p in root.rglob("*.jsonl")
        if p.is_file() and _SESSION_FILE_RE.match(p.name)
    ]
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


def _entry_ts(entry: dict) -> float | None:
    """ISO timestamp of an entry as epoch seconds, None when absent/unparseable."""
    raw = str(entry.get("timestamp") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tokens_of(text: str) -> set:
    return set(_TOKEN_RE.findall(text.lower()))


def _segments(entries: list[dict]) -> list[dict]:
    """Split a session log into events at time gaps and topic shifts.

    Boundary when consecutive entries sit more than EVENT_GAP_MINUTES apart
    (from entry timestamps), or when a user turn's token overlap with the
    running event drops below EVENT_TOPIC_OVERLAP (jaccard, db._tokens
    vocabulary). Entries without timestamps follow the current segment.
    """
    segments: list[dict] = []
    cur: dict | None = None
    last_ts: float | None = None
    last_turn_tokens: set | None = None
    for e in entries:
        ts = _entry_ts(e)
        is_user_turn = e.get("type") == "message" and \
            (e.get("message") or {}).get("role") == "user"
        turn_tokens: set | None = None
        if is_user_turn:
            txt = _text_of(e.get("message") or {}).strip()
            if txt and not _CONTENT_SKIP.search(txt):
                turn_tokens = _tokens_of(txt)
        boundary = cur is None
        if not boundary and ts is not None and last_ts is not None:
            if ts - last_ts > EVENT_GAP_MINUTES * 60.0:
                boundary = True
        if not boundary and turn_tokens and last_turn_tokens and \
                len(turn_tokens | last_turn_tokens):
            overlap = len(turn_tokens & last_turn_tokens) / \
                len(turn_tokens | last_turn_tokens)
            if overlap < EVENT_TOPIC_OVERLAP:
                boundary = True
        if turn_tokens is not None:
            last_turn_tokens = turn_tokens
        # The entry that opens a new event belongs to it; without this the
        # boundary user turn is dropped and the next overlap check compares
        # against the previous event's last turn again.
        if boundary:
            cur = {"entries": [e], "start": ts, "end": ts, "index": len(segments)}
            segments.append(cur)
        else:
            cur["entries"].append(e)
            if ts is not None:
                cur["end"] = ts
        if cur is not None and ts is not None:
            last_ts = ts
    return segments


def _failure_line(raw: str) -> str:
    """Distil a toolResult body into the one line that says what failed.

    A traceback is five lines of boilerplate around one message; a test
    summary is one FAIL: line. The shortest line carrying an error-shaped
    signal is usually the message, not the frames around it.
    """
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    hits = [ln for ln in lines if _FAILURE_RE.search(ln)]
    if not hits:
        return raw
    priority = sorted(hits, key=lambda ln: (
        0 if re.search(r"(Error|Exception|FAIL)\s*:", ln) else 1,
        len(ln),
    ))
    return priority[0]


def _event_candidates(seg: dict, session_id: str, project_id: str,
                      max_failures: int, max_files: int) -> list[dict]:
    """Extraction rules for one event segment.

    Mirrors the original per-session rules, but scoped to the segment's
    entries: failures, files, and decision keywords inside this event only.
    Each candidate carries the event id and an event tag so replay can
    filter one episode without re-parsing the log.
    """
    user_turns: list[str] = []
    failures: list[str] = []
    files: "OrderedDict[str, int]" = OrderedDict()

    for e in seg["entries"]:
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
                if (name not in _READ_TOOLS and _FAILURE_RE.search(body)
                        and not _CONTENT_SKIP.search(body)):
                    failures.append(f"{name}: {_clip(_failure_line(body), 200)}")
                # track file tool calls
                if name in ("bash", "shell"):
                    for word in _TOKEN_RE.findall(body):
                        if "/" in word and word.startswith("/"):
                            files[word] = files.get(word, 0) + 1
        # Custom entries carry file paths
        if t == "custom":
            payload = e.get("custom") or e.get("data") or {}
            if not isinstance(payload, dict):
                payload = {}
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

    event_id = f"{session_id}#e{seg['index']}"
    event_tag = f"{EVENT_TAG_PREFIX}{seg['index']}"
    candidates: list[dict] = []

    # Failure patterns -> INFERENCE (what went wrong, with confidence floor)
    for failure in failures[:max_failures]:
        lesson = _failure_line(failure)
        # The matcher catches any line mentioning an error; only a line
        # that names the error type carries a lesson. Bare "Command
        # exited with code N" or a parse-error fragment is shell noise,
        # and a typo session must not become permanent memory.
        if _NOISE_RE.search(lesson) or not _LESSON_RE.search(lesson):
            continue
        if _PYTEST_RE.search(lesson):
            continue
        candidates.append({
            "rtype": CAPTURE_DEFAULT_TYPE,
            "content": f"Observed failure: {lesson}",
            "evidence": _clip(failure, 200),
            "confidence": CAPTURE_DEFAULT_CONFIDENCE,
            "source": "auto-capture",
            "session_id": session_id,
            "project_id": project_id,
            "tags": f"failure,auto-capture,{event_tag}",
            "event_id": event_id,
        })

    # File patterns -> ASSUMPTION (which files were worked on)
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
            "tags": f"files,auto-capture,{event_tag}",
            "event_id": event_id,
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
                "tags": f"decision,auto-capture,{event_tag}",
                "event_id": event_id,
            })
    return candidates


def extract_lessons(
    entries: list[dict],
    session_path: Path | None = None,
    max_failures: int = MAX_FAILURES_STORED,
    max_files: int = MAX_FILES_STORED,
) -> list[dict]:
    """Turn a session log into a list of candidate memory records.

    The log is first segmented into events (time gap or topic shift, see
    _segments); each event yields its own candidates carrying that event's
    id and tag, so a midnight deploy failure does not inherit the memory
    tags of the refactor that followed at breakfast.

    Each candidate is a dict with the keys that db.upsert_record() needs:
        rtype, content, evidence, confidence, source, session_id,
        project_id, tags, event_id.

    The caller decides whether to write them (apply=True) or just inspect them.
    """
    session_id = session_path.stem if session_path else ""
    project_id = session_path.parent.name if session_path else ""

    candidates: list[dict] = []
    for seg in _segments(entries):
        candidates.extend(
            _event_candidates(seg, session_id, project_id,
                              max_failures, max_files))

    # Never persist a candidate whose content or evidence carries a secret:
    # toolResult bodies can contain env dumps, tokens in tracebacks, etc.
    # The mcp remember path scans; auto-capture must not bypass it.
    kept = []
    for cand in candidates:
        leaked = scan_text(cand["content"] + " " + str(cand.get("evidence") or ""))
        if leaked:
            continue
        kept.append(cand)
    return kept


def capture_session(
    db_mod,
    conn,
    session_path: Path | None = None,
    apply: bool = False,
    dry_run: bool = False,
    reflect: bool = False,
) -> dict:
    """Run extraction and optionally write candidates to the DB.

    db_mod: the omp_memory.db module (passed to avoid circular import).
    conn: an open, inited sqlite3.Connection.
    session_path: path to the session .jsonl; default = latest_session().
    apply: write candidates to DB (default False = dry_run mode).
    dry_run: alias for apply=False (explicit).
    reflect: after writing, run the experience pass so new lessons are
        abstracted into patterns in the same transaction.

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
            confidence=min(cand["confidence"], CAPTURE_CONFIDENCE_CEILING),
            source=cand["source"],
            tags=cand["tags"],
            session_id=cand["session_id"],
            project_id=cand["project_id"],
            event_id=cand.get("event_id") or "",
        )
        written += 1
    conn.commit()
    reflected = None
    if reflect and apply:
        from . import experience
        reflected = experience.reflect(conn, apply=True)
    report = {
        "session": str(session_path),
        "candidates": len(candidates),
        "written": written,
        "apply": True,
    }
    if reflected is not None:
        report["reflect"] = reflected
    return report
