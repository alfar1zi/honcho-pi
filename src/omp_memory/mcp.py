#!/usr/bin/env python3
"""
honcho-mcp - Model Context Protocol server for the omp-memory system.

One codebase, two transports:

    stdio  (default)   an agent spawns this per session; single-user local.
    http               self-hosted; many users, bearer-token auth, per-token
                       database isolation under <data-dir>/users/<hash>/.

Why this exists: the storage layer answers "where do memories live"; the MCP
server is what makes an agent actually *use* them. Without it, memories are a
file that grows and is never read.

Tools
    recall      Search memory before acting. Ranked by lexical match,
                confidence, heat and type prior.
    remember    Persist a fact/decision/correction learned this session.
    reinforce   Confirm or refute an existing memory after observing reality.
    forget      Retire a memory that is provably wrong.
    stats       Inspect what the agent has learned.

Environment
    OMP_MEMORY_DB          explicit DB path (wins over all else)
    HONCHO_HOME            data root (default ~/.omp/profiles/<profile>/agent/honcho)
    HONCHO_TRANSPORT       stdio | http            (default stdio)
    HONCHO_HOST/PORT       http bind               (default 127.0.0.1:8765)
    HONCHO_TOKENS          comma-separated bearer tokens for http mode
    HONCHO_MAX_RECALL      default recall limit     (default 8)
    HONCHO_EXTRA_TOOLS     os.pathsep list of adapter directories
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import db
from .context import _neutralize_fence

SERVER_NAME = "honcho"
SERVER_VERSION = "2.0.0"
PROTOCOL_VERSION = "2024-11-05"

MAX_CONTENT = 4000
MAX_RECALL_DEFAULT = int(os.environ.get("HONCHO_MAX_RECALL", "8"))

# Last recall result per user ("" for single-tenant). Reinforce without an id
# targets these: the agent that just used a recalled memory does not always
# know which record it came from, but it does know the recall it acted on.
_last_recall: Dict[str, List[str]] = {}
_recalled_users: set = set()


# --------------------------------------------------------------------------
# Paths and connection
# --------------------------------------------------------------------------

def _default_home() -> Path:
    explicit = os.environ.get("HONCHO_HOME")
    if explicit:
        return Path(explicit).expanduser()
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    if agent_dir:
        return Path(agent_dir) / "honcho"
    profile = os.environ.get("OMP_PROFILE") or "default"
    return Path.home() / ".omp" / "profiles" / profile / "agent" / "honcho"


def resolve_db_path(user: str = "") -> Path:
    """Per-user database path. Empty user -> single-tenant layout."""
    explicit = os.environ.get("OMP_MEMORY_DB")
    home = _default_home()
    if user:
        # Token identities must never collide, and must not leak into a path
        # name a filesystem dislikes; hash them.
        return home / "users" / db.sha(f"user:{user}") / "memory.db"
    if explicit:
        return Path(explicit).expanduser()
    return home / "memory.db"

_writer_lock = threading.RLock()
_local = threading.local()


def _stale_schema(conn) -> bool:
    """True when the cached connection is pinned to a dead schema.

    A migration rebuilds the records table (new inode on some filesystems),
    but the already-open connection keeps the old schema in its schema
    cookie, so every later statement fails with "malformed database schema".
    A cheap probe statement is the detection: it costs a parse, not a scan.
    """
    try:
        conn.execute("SELECT 1 FROM records LIMIT 1").fetchone()
        return False
    except sqlite3.DatabaseError as exc:
        # Only the stale-cache signature. "database disk image is malformed"
        # (page corruption) also contains "malformed" but is a file problem:
        # a reopen fixes nothing, and init_db's auto-repair would swap the
        # live DB for a backup on every call.
        return str(exc).lower().startswith("malformed database schema")


def get_conn(user: str = ""):
    """Open one sqlite connection per calling thread.

    sqlite3 connections are not thread-safe by default: sharing one across the
    ThreadingHTTPServer worker threads made the second request fail with
    ProgrammingError, and the failure was permanent for the process. Each
    thread opens its own connection; WAL plus the busy timeout lets them
    coexist. init_db() runs under the writer lock so concurrent first touches
    cannot race the migration.

    A cached connection whose schema cookie is stale (the file was migrated
    by another process while this one held the old handle) is closed and
    reopened once: the old handle would otherwise fail every tool call
    forever, because the error lives in the connection, not the file.
    """
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    key = str(resolve_db_path(user))
    conn = conns.get(key)
    if conn is not None:
        if _stale_schema(conn):
            try:
                conn.close()
            except sqlite3.Error:
                pass
            conns.pop(key, None)
        else:
            return conn
    path = Path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        conn = db.connect(key)
        # init_db() returns None in this API; it migrates in place.
        db.init_db(conn, key)
    conns[key] = conn
    return conn


def with_conn(user: str = "", fn=None):
    """Run a tool body under the single-writer lock and return its result.

    Write work (remember/reinforce/forget) is serialised so concurrent HTTP
    callers cannot interleave the read-modify-write of a merge. Read work also
    takes the lock: it is cheaper than teaching every tool which statements
    need it, and recall under load is not the bottleneck.
    """
    def run(body):
        with _writer_lock:
            return body(get_conn(user))
    if fn is not None:
        return run(fn)
    return run


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

def _clip(text: str, limit: int = MAX_CONTENT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


_MEM_TOOLS = {"recall", "remember", "reinforce", "forget", "stats", "handoff", "maintenance", "capture"}


def _auto_recall_block(user: str, tool_name: str, args: Dict[str, Any]) -> str:
    """Run one recall for the session's first mem tool call, return its text."""
    query = " ".join(
        str(v) for v in [tool_name] + list(args.values()) if v
    ).strip()[:200]
    try:
        conn = get_conn(user)
        try:
            rows = db.recall(conn, query, limit=4, full=True)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - injection must never break the tool
        return ""
    if not rows:
        return ""
    _recalled_users.add(user)
    out = ["[Auto-recall] matching memories for this session's first call:"]
    for r in rows:
        out.append(
            f"- [{r['type']}] {_clip(r['content'], 300)} "
            f"(id={r['id']} conf={float(r['confidence']):.2f})"
        )
    out.append("Use mcp__mem_recall for more, reinforce after relying on one.")
    return "\n".join(out) + "\n\n"


def tool_recall(args: Dict[str, Any], user: str = "") -> str:
    def body(conn):
        query = str(args.get("query") or "").strip()
        if not query:
            return "recall: query is required"
        limit = int(args.get("limit") or MAX_RECALL_DEFAULT)
        limit = max(1, min(limit, 50))
        rtype = args.get("type")
        project = args.get("project")
        session = args.get("session")
        verified_only = bool(args.get("verified_only"))
        min_confidence = float(args.get("min_confidence") or 0.0)
        about_peer = args.get("about_peer")
        # recall() reranks and takes the same confidence filters, so every
        # query path keeps the same ranking instead of silently downgrading
        # filtered queries to plain lexical order.
        rows = db.recall(conn, query, limit=limit, full=True,
                         rtype=rtype, project_id=project,
                         session_id=session,
                         verified_only=verified_only,
                         min_confidence=min_confidence,
                         about_peer=about_peer, as_of=args.get("as_of"))
        if not rows:
            return f"No memory matched: {query!r}"
        _last_recall[user] = [r["id"] for r in rows]
        # Retrieval practice: the act of recalling is itself a learning
        # event. Logged as a zero-delta signal so it starts the spacing
        # clock and feeds consolidation without moving confidence.
        db.log_retrieval(conn, _last_recall[user])
        if args.get("brief"):
            # L0 progressive disclosure: index only, token cost visible.
            out = [f"Recalled {len(rows)} record(s) for {query!r} (brief index):"]
            for r in rows:
                first = next((l for l in r["content"].splitlines() if l.strip()), "")
                out.append(f"- {r['id']} [{r['type']}] {first[:100]} "
                           f"(conf {float(r['confidence']):.2f})")
            body = "\n".join(out)
            out.append(f"~{len(body) // 4} tokens. Detail: recall with a narrower "
                       "query, or remember the id and ask for it specifically.")
            return "\n".join(out)
        out = [f"Recalled {len(rows)} memory record(s) for {query!r}:"]
        for r in rows:
            flag = "verified" if r["verified"] else "unverified"
            out.append(
                f"- [{r['type']}] {_clip(r['content'], 500)}\n"
                f"  id={r['id']} confidence={float(r['confidence']):.2f} {flag}"
                + (f" evidence={_clip(r['evidence'], 200)}" if r["evidence"] else "")
            )
        cf = db.conflicts(conn, [r["id"] for r in rows])
        if cf:
            out.append("Possible contradictions (judge, do not auto-merge):")
            for c in cf:
                out.append(f"- {c['a']} vs {c['b']} ({c['kind']}, "
                           f"overlap {c['overlap']})")
        stale = [r["id"] for r in rows if r.get("stale")]
        if stale:
            out.append(f"Stale anchors (source file changed, re-verify): {', '.join(stale)}")
        out.append("Say reinforce {id, signal} after using one of these: success when it "
                   "held, failure when it did not, correction when the right answer was "
                   "different. The signal feeds confidence and heat.")
        return "\n".join(out)
    return with_conn(user, body)


def tool_digest(args: Dict[str, Any], user: str = "") -> str:
    def body(conn):
        budget = max(200, min(int(args.get("budget") or 2000), 20000))
        d = db.digest(conn, budget=budget, project_id=args.get("project"))
        return d["text"]
    return with_conn(user, body)

def tool_remember(args: Dict[str, Any], user: str = "") -> str:
    def body(conn):
        content = _neutralize_fence(str(args.get("content") or "").strip())
        if not content:
            return "remember: content is required"
        rtype = str(args.get("type") or "FACT").upper()
        if rtype not in db.VALID_TYPES:
            return f"remember: invalid type {rtype}; use one of {sorted(db.VALID_TYPES)}"
        leaked = _scan_secret(content + " " + str(args.get("evidence") or ""))
        if leaked:
            return f"remember: refused, possible secret detected ({leaked})"
        result = db.remember(
            conn,
            rtype=rtype,
            content=_clip(content),
            evidence=_clip(str(args.get("evidence") or "")),
            verified=1 if args.get("verified") else 0,
            confidence=float(args.get("confidence") if args.get("confidence") is not None else 0.6),
            source=str(args.get("source") or "agent"),
            tags=str(args.get("tags") or ""),
            session_id=str(args.get("session") or ""),
            project_id=str(args.get("project") or ""),
            about_peer=str(args.get("about_peer") or ""),
            importance=float(args.get("importance") if args.get("importance") is not None else 0.5),
            cue=str(args.get("cue") or ""),
        )
        rid = result["id"]
        # Temporal validity + code anchor (v15): set after the write so the
        # content identity path stays untouched. anchor_sha defaults to the
        # file's current hash so check_anchors has a baseline.
        anchor_path = str(args.get("anchor_path") or "")
        valid_from = str(args.get("valid_from") or "")
        valid_until = str(args.get("valid_until") or "")
        if anchor_path or valid_from or valid_until:
            import hashlib
            sha = ""
            if anchor_path:
                try:
                    with open(anchor_path, "rb") as f:
                        sha = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    return f"remember: anchor_path not readable: {anchor_path}"
            conn.execute(
                "UPDATE records SET anchor_path=?, anchor_sha=?, valid_from=?,"
                " valid_until=? WHERE id=?",
                (anchor_path, sha, valid_from, valid_until, rid))
            conn.commit()
        rec = db.get_record(conn, rid) or {}
        out = (
            f"Stored {rtype} id={rid} confidence={float(rec.get('confidence', 0)):.2f} "
            f"verified={'yes' if rec.get('verified') else 'no'} ({result['applied']})"
        )
        # Near-duplicates were reported, not merged; the caller should read this.
        if result.get("advisory"):
            out += f"\n{result['advisory']}"
        return out
    return with_conn(user, body)


def tool_reinforce(args: Dict[str, Any], user: str = "") -> str:
    def body(conn):
        rid = str(args.get("id") or "").strip()
        if not rid:
            # No explicit id: the agent is confirming the recall it just used.
            ids = _last_recall.get(user) or []
            if not ids:
                return "reinforce: id is required (or call recall first)"
            if len(ids) > 1:
                return ("reinforce: id is required; the last recall returned multiple "
                        "records, so name the one you used")
            rid = ids[0]
        signal = str(args.get("signal") or "").strip().lower()
        if signal not in db.VALID_SIGNALS:
            return f"reinforce: invalid signal {signal}; use one of {sorted(db.VALID_SIGNALS)}"
        content = _neutralize_fence(str(args.get("content") or "").strip())
        evidence = _neutralize_fence(str(args.get("evidence") or "").strip())
        if content:
            leaked = _scan_secret(content + " " + evidence)
            if leaked:
                return f"reinforce: {leaked} detected in correction content"
        try:
            new_conf = db.reinforce(conn, rid, signal,
                                    content=content or None,
                                    evidence=evidence,
                                    source=str(args.get("source") or "recall"))
        except ValueError as exc:
            return f"reinforce: {exc}"
        out = f"Reinforced {rid} with {signal} -> confidence {new_conf:.2f}"
        promo = db.last_promotion()
        if promo:
            out += f"\nPromoted to {promo['to_type']} (heat {float(promo['heat']):.2f})"
        return out
    return with_conn(user, body)


def tool_forget(args: Dict[str, Any], user: str = "") -> str:
    def body(conn):
        rid = str(args.get("id") or "").strip()
        if not rid:
            return "forget: id is required"
        rec = db.get_record(conn, rid)
        if not rec:
            return f"forget: no record {rid}"
        # reinforcement rows hold an FK to records; delete children first or the
        # parent delete fails with FOREIGN KEY constraint failed. The graveyard
        # row is written before the delete so a crash leaves an audit trail
        # instead of a silently missing record, matching forget_cold(). REPLACE
        # because the same content can be forgotten, re-created and forgotten
        # again; a plain INSERT would then fail on the PRIMARY KEY.
        conn.execute(
            "INSERT OR REPLACE INTO forgotten_graveyard (id, type, content, reason, ts)"
            " SELECT id, type, content, 'forgotten via MCP', ? FROM records WHERE id=?",
            (db.now_iso(), rid),
        )
        conn.execute("DELETE FROM reinforcement WHERE record_id=?", (rid,))
        conn.execute("DELETE FROM records WHERE id=?", (rid,))
        conn.commit()
        return f"Forgot {rid} ({rec['type']})"
    return with_conn(user, body)


def tool_stats(args: Dict[str, Any], user: str = "") -> str:
    def body(conn):
        return json.dumps(db.stats(conn), indent=2, sort_keys=True)
    return with_conn(user, body)


def tool_maintenance(args: Dict[str, Any], user: str = "") -> str:
    def body(conn):
        op = str(args.get("op") or "decay").strip().lower()
        if op == "decay":
            report = {"decayed": db.apply_decay(conn),
                      "consolidated": db.consolidate(conn)}
            return json.dumps(report, indent=2, sort_keys=True)
        if op == "sleep":
            report = db.sleep_pass(conn, apply=bool(args.get("apply", True)))
            report["anchors"] = db.check_anchors(conn, apply=bool(args.get("apply", True)))
            return json.dumps(report, indent=2, sort_keys=True)
        if op == "forget_cold":
            report = db.forget_cold(conn, apply=bool(args.get("apply")))
            return json.dumps(report, indent=2, sort_keys=True)
        if op == "promote":
            return json.dumps({"promotion": db.promote_hot(conn)}, indent=2, sort_keys=True)
        if op == "reflect":
            from . import experience
            report = experience.reflect(conn, apply=bool(args.get("apply", True)))
            return json.dumps(report, indent=2, sort_keys=True)
        if op == "capture":
            # Lazy import: capture reads harness session logs, which are not
            # the storage layer's concern.
            from . import capture
            report = capture.capture_session(
                db, conn, apply=bool(args.get("apply")),
                reflect=bool(args.get("reflect")))
            return json.dumps(report, indent=2, sort_keys=True)
        return f"maintenance: unknown op {op}; use decay | sleep | reflect | forget_cold | promote | capture"
    return with_conn(user, body)
def _scan_secret(text: str) -> Optional[str]:
    # One pattern list for every write door (capture, MCP, web).
    from .capture import scan_text
    hits = scan_text(text)
    return hits[0][0] if hits else None

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "recall",
        "description": (
            "Search persistent memory from previous sessions before starting work. "
            "Returns ranked records with type, confidence and evidence. Call this when a "
            "task may depend on prior decisions, corrections, project conventions or "
            "hard-won debugging facts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords describing what you need."},
                "limit": {"type": "integer", "description": "Max results (1-50).", "default": 8},
                "type": {"type": "string", "enum": sorted(db.VALID_TYPES)},
                "project": {"type": "string", "description": "Scope to a project id."},
                "session": {"type": "string", "description": "Scope to one session's records."},
                "verified_only": {"type": "boolean"},
                "min_confidence": {"type": "number"},
                "brief": {"type": "boolean",
                          "description": "L0 index only: id + one line + confidence, "
                                         "with token cost. Fetch detail via recall full "
                                         "or a narrower query."},
                "as_of": {"type": "string",
                          "description": "ISO timestamp: what was true then "
                                         "(bi-temporal validity filter)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Persist something learned that will matter in future sessions: a decision, "
            "a root cause, a project convention, a correction. Use FACT only when verified; "
            "otherwise INFERENCE or ASSUMPTION. Keep content short and self-contained."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "One self-contained statement."},
                "type": {"type": "string", "enum": sorted(db.VALID_TYPES), "default": "FACT"},
                "evidence": {"type": "string", "description": "How this was verified."},
                "verified": {"type": "boolean", "default": False},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.6},
                "tags": {"type": "string"},
                "project": {"type": "string"},
                "source": {"type": "string"},
                "session": {"type": "string"},
                "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5,
                               "description": "How much this intent matters (INTENT records)."},
                "cue": {"type": "string", "description": "Trigger phrase that surfaces an INTENT on recall."},
                "anchor_path": {"type": "string",
                                "description": "File this lesson describes; its sha is "
                                               "recorded so a later change flags the record stale."},
                "valid_from": {"type": "string", "description": "ISO: valid from this moment."},
                "valid_until": {"type": "string", "description": "ISO: expires at this moment."},
            },
            "required": ["content"],
        },
    },
    {
        "name": "reinforce",
        "description": (
            "Adjust a memory's confidence after observing reality. signal=success when it "
            "held, failure when it did not, correction when you learned the right answer, "
            "decay to fade it. This is how memory stays calibrated instead of trusting stale "
            "notes. Omit id to confirm the single record from your last recall."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string",
                       "description": "record id; defaults to the last single-hit recall"},
                "signal": {"type": "string", "enum": sorted(db.VALID_SIGNALS)},
                "source": {"type": "string"},
                "content": {"type": "string",
                            "description": ("correction only: the corrected "
                                            "statement; requires evidence or a "
                                            "verified record")},
                "evidence": {"type": "string",
                             "description": "why the correction is true"},
            },
            "required": ["signal"],
        },
    },
    {
        "name": "forget",
        "description": ("Delete a memory record that is provably wrong. "
                       "Prefer reinforce(failure) when merely uncertain."),
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "stats",
        "description": "Counts of stored memory by type, plus verified totals.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "maintenance",
        "description": ("Housekeeping: op=decay ages confidence, op=sleep runs "
                       "the consolidation pass, op=reflect abstracts cross-session "
                       "patterns into PROCEDURAL/REFLECTIVE records, op=forget_cold "
                       "reports (or with apply=true deletes) untouched cold records, "
                       "op=promote reconsiders promotion of hot INFERENCE/ASSUMPTION, "
                       "op=capture extracts lessons from the current session log "
                       "(with apply=true they are written to memory)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string",
                       "enum": ["decay", "sleep", "reflect", "forget_cold",
                                "promote", "capture"]},
                "apply": {"type": "boolean",
                          "description": "forget_cold: actually delete; capture/reflect: write"},
            },
        },
    },
    {
        "name": "digest",
        "description": (
            "The always-in-context memory block: top verified rules, decisions and "
            "facts rendered into a char budget, stable across calls so prefix caches "
            "survive. Inject once at session start; use recall for task-specific hits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "budget": {"type": "integer", "description": "Char budget.", "default": 2000},
                "project": {"type": "string", "description": "Scope to a project id."},
            },
        },
    },
]

_HANDLERS: Dict[str, Any] = {
    "recall": tool_recall,
    "remember": tool_remember,
    "reinforce": tool_reinforce,
    "forget": tool_forget,
    "maintenance": tool_maintenance,
    "stats": tool_stats,
    "digest": tool_digest,
}


def _load_extensions() -> None:
    """Merge optional tools from HONCHO_EXTRA_TOOLS directories.

    Some capabilities are inherently harness-specific. A session-transcript
    reader, for example, only works where the transcript format is known, and
    those formats are undocumented and change without notice. Shipping one in
    the package would mean advertising a feature that silently breaks.

    Instead an operator can drop a module into a directory and point
    HONCHO_EXTRA_TOOLS at it. Each module exposes `TOOLS` (a list of MCP tool
    definitions) and `call(args, user) -> str`. Failures are reported on stderr
    and never prevent the core tools from loading.
    """
    raw = os.environ.get("HONCHO_EXTRA_TOOLS", "")
    for entry in [p for p in raw.split(os.pathsep) if p]:
        d = Path(entry).expanduser()
        if not d.is_dir():
            continue
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
        for mod_path in sorted(d.glob("*.py")):
            name = mod_path.stem
            if name.startswith("_"):
                continue
            # Only modules that opt in as adapters are loaded; a helper module
            # dropped in the same directory must not be imported for side effects.
            if not mod_path.name.startswith("local_") and not mod_path.name.startswith("adapter_"):
                continue
            try:
                import importlib
                mod = importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[honcho] extension {name} failed: {exc}\n")
                continue
            mod_tools = getattr(mod, "TOOLS", None)
            call = getattr(mod, "call", None)
            if not mod_tools or not callable(call):
                continue
            for spec in mod_tools:
                tool_name = spec.get("name")
                if not tool_name or tool_name in _HANDLERS:
                    continue
                TOOLS.append(spec)
                _HANDLERS[tool_name] = _make_extension_handler(call)
                sys.stderr.write(f"[honcho] extension tool loaded: {tool_name}\n")


def _make_extension_handler(call):
    def handler(args: Dict[str, Any], user: str = "") -> str:
        return call(args, user)
    return handler


_load_extensions()


INSTRUCTIONS = (
    "honcho memory is your persistent memory across sessions and model switches. "
    "Three habits, every session, no exceptions. "
    "(1) Before any non-trivial work, call `recall` with keywords about the task; "
    "prior decisions and corrections are usually more reliable than re-deriving them. "
    "(2) After a recalled record proves right or wrong in this session, call `reinforce` "
    "with success/failure for the id you used, so confidence stays calibrated and hot "
    "records get promoted while stale ones decay. When you learn something durable "
    "(a root cause, a convention, a verified fact), call `remember` with an honest type. "
    "(3) At the end of a session, call maintenance {op: capture, apply: true} to persist "
    "lessons from the transcript as low-confidence candidates. When you suspect you are "
    "a continuation after a model switch or context reset, call `handoff` before asking "
    "the operator anything. Never store secrets."
)


# --------------------------------------------------------------------------
# JSON-RPC dispatch (shared by both transports)
# --------------------------------------------------------------------------

def _result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_message(msg: Any, user: str = "") -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message. Returns None for notifications."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "invalid request: message must be an object")
    try:
        return _handle_message(msg, user)
    except Exception:  # noqa: BLE001 - one bad message must not kill the loop
        traceback.print_exc(file=sys.stderr)
        req_id = msg.get("id") if isinstance(msg, dict) else None
        if req_id is None:
            return None
        return _error(req_id, -32603, "internal error handling message")


def _handle_message(msg: Dict[str, Any], user: str = "") -> Optional[Dict[str, Any]]:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = _HANDLERS.get(name)
        if handler is None:
            return _error(req_id, -32602, f"unknown tool: {name}")
        try:
            text = handler(args, user)
            if name not in _MEM_TOOLS and user not in _recalled_users:
                block = _auto_recall_block(user, name, args)
                if block:
                    text = block + text
            return _result(req_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # noqa: BLE001 - surface, never crash the server
            traceback.print_exc(file=sys.stderr)
            return _result(req_id, {
                "content": [{"type": "text", "text": f"tool {name} failed: {exc}"}],
                "isError": True,
            })

    if method in ("resources/list", "prompts/list"):
        return _result(req_id, {"resources": []} if "resources" in method else {"prompts": []})

    if req_id is None:
        return None
    return _error(req_id, -32601, f"method not found: {method}")


# --------------------------------------------------------------------------
# Transport: stdio
# --------------------------------------------------------------------------

def serve_stdio() -> None:
    """Newline-delimited JSON-RPC on stdin/stdout, as MCP stdio expects."""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, list):
            out = [r for r in (handle_message(m) for m in msg) if r is not None]
            if out:
                sys.stdout.write(json.dumps(out) + "\n")
        else:
            resp = handle_message(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------
# Transport: http (streamable single-POST JSON-RPC, bearer auth)
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    tokens: set = set()

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        sys.stderr.write("[honcho-http] " + (fmt % args) + "\n")

    def _user(self) -> Optional[str]:
        if not self.tokens:
            return ""
        header = self.headers.get("Authorization", "").strip()
        if not header.lower().startswith("bearer "):
            return None
        token = header[7:].strip()
        return token if token in self.tokens else None

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz"):
            self._send(200, {"ok": True, "server": SERVER_NAME, "version": SERVER_VERSION})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        user = self._user()
        if user is None:
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            self._send(413, {"error": "payload too large"})
            return
        try:
            msg = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return
        if isinstance(msg, list):
            out = [r for r in (handle_message(m, user) for m in msg) if r is not None]
            self._send(200, out)
        else:
            resp = handle_message(msg, user)
            self._send(200, resp or {})
        return


def serve_http() -> None:
    host = os.environ.get("HONCHO_HOST", "127.0.0.1")
    port = int(os.environ.get("HONCHO_PORT", "8765"))
    tokens = {t.strip() for t in os.environ.get("HONCHO_TOKENS", "").split(",") if t.strip()}
    _Handler.tokens = tokens
    if host not in ("127.0.0.1", "localhost", "::1") and not tokens:
        sys.stderr.write(
            "[honcho-http] refusing to bind a public interface without HONCHO_TOKENS\n"
        )
        raise SystemExit(2)
    srv = ThreadingHTTPServer((host, port), _Handler)
    sys.stderr.write(
        f"[honcho-http] listening on http://{host}:{port} "
        f"({'token auth' if tokens else 'no auth, loopback only'})\n"
    )
    srv.serve_forever()


def main() -> None:
    transport = os.environ.get("HONCHO_TRANSPORT", "stdio").lower()
    if transport == "http":
        serve_http()
    else:
        serve_stdio()


if __name__ == "__main__":
    main()
