#!/usr/bin/env python3
"""
honcho-mcp - Model Context Protocol server for the honcho memory system.

One codebase, two transports:

    stdio  (default)   omp spawns this per session; single-user local.
    http               self-hosted; many users, bearer-token auth, per-token
                       database isolation under <data-dir>/users/<hash>/.

Why this exists: the storage layer answers "where do memories live"; the MCP
server is what makes an agent actually *use* them. Without it, memories are a
file that grows and is never read.

Tools
    recall      Search memory before acting. Ranked by relevance+confidence.
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
    HONCHO_MAX_RECALL      default recall limit   (default 8)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import memory_db as db  # noqa: E402

SERVER_NAME = "honcho"
SERVER_VERSION = "2.0.0"
PROTOCOL_VERSION = "2024-11-05"

MAX_CONTENT = 4000
MAX_RECALL_DEFAULT = int(os.environ.get("HONCHO_MAX_RECALL", "8"))


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


_lock = threading.Lock()
_conns: Dict[str, Any] = {}


def get_conn(user: str = ""):
    path = resolve_db_path(user)
    key = str(path)
    with _lock:
        conn = _conns.get(key)
        if conn is not None:
            return conn
        path.parent.mkdir(parents=True, exist_ok=True)
        old_db = db.DB_PATH
        db.DB_PATH = key
        try:
            conn = db.connect(key)
            conn = db.init_db(conn, key)
        finally:
            db.DB_PATH = old_db
        _conns[key] = conn
        return conn


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

def _clip(text: str, limit: int = MAX_CONTENT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def tool_recall(args: Dict[str, Any], user: str = "") -> str:
    conn = get_conn(user)
    query = str(args.get("query") or "").strip()
    if not query:
        return "recall: query is required"
    limit = int(args.get("limit") or MAX_RECALL_DEFAULT)
    limit = max(1, min(limit, 50))
    rows = db.search(
        conn,
        query,
        limit=limit,
        rtype=args.get("type"),
        project_id=args.get("project"),
        verified_only=bool(args.get("verified_only")),
        min_confidence=float(args.get("min_confidence") or 0.0),
    )
    if not rows:
        return f"No memory matched: {query!r}"
    out = [f"Recalled {len(rows)} memory record(s) for {query!r}:"]
    for r in rows:
        flag = "verified" if r["verified"] else "unverified"
        out.append(
            f"- [{r['type']}] {_clip(r['content'], 500)}\n"
            f"  id={r['id']} confidence={float(r['confidence']):.2f} {flag}"
            + (f" evidence={_clip(r['evidence'], 200)}" if r["evidence"] else "")
        )
    return "\n".join(out)


def tool_remember(args: Dict[str, Any], user: str = "") -> str:
    conn = get_conn(user)
    content = str(args.get("content") or "").strip()
    if not content:
        return "remember: content is required"
    rtype = str(args.get("type") or "FACT").upper()
    if rtype not in db.VALID_TYPES:
        return f"remember: invalid type {rtype}; use one of {sorted(db.VALID_TYPES)}"
    leaked = _scan_secret(content + " " + str(args.get("evidence") or ""))
    if leaked:
        return f"remember: refused, possible secret detected ({leaked})"
    rid = db.upsert_record(
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
    )
    rec = db.get_record(conn, rid) or {}
    return (
        f"Stored {rtype} id={rid} confidence={float(rec.get('confidence', 0)):.2f} "
        f"verified={'yes' if rec.get('verified') else 'no'}"
    )


def tool_reinforce(args: Dict[str, Any], user: str = "") -> str:
    conn = get_conn(user)
    rid = str(args.get("id") or "").strip()
    signal = str(args.get("signal") or "").strip().lower()
    if not rid:
        return "reinforce: id is required"
    if signal not in db.VALID_SIGNALS:
        return f"reinforce: invalid signal {signal}; use one of {sorted(db.VALID_SIGNALS)}"
    try:
        new_conf = db.reinforce(conn, rid, signal, source=str(args.get("source") or "agent"))
    except ValueError as exc:
        return f"reinforce: {exc}"
    return f"Reinforced {rid} with {signal} -> confidence {new_conf:.2f}"


def tool_forget(args: Dict[str, Any], user: str = "") -> str:
    conn = get_conn(user)
    rid = str(args.get("id") or "").strip()
    if not rid:
        return "forget: id is required"
    rec = db.get_record(conn, rid)
    if not rec:
        return f"forget: no record {rid}"
    # reinforcement rows hold an FK to records; delete children first or the
    # parent delete fails with FOREIGN KEY constraint failed.
    conn.execute("DELETE FROM reinforcement WHERE record_id=?", (rid,))
    conn.execute("DELETE FROM records WHERE id=?", (rid,))
    conn.commit()
    return f"Forgot {rid} ({rec['type']})"


def tool_stats(args: Dict[str, Any], user: str = "") -> str:
    conn = get_conn(user)
    s = db.stats(conn)
    return json.dumps(s, indent=2, sort_keys=True)


_SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9_-]{20,}", "openai-style key"),
    (r"ghp_[A-Za-z0-9]{20,}", "github token"),
    (r"AKIA[0-9A-Z]{16}", "aws key id"),
    (r"(?i)bearer\s+[A-Za-z0-9._-]{20,}", "bearer token"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
]


def _scan_secret(text: str) -> Optional[str]:
    import re
    for pattern, label in _SECRET_PATTERNS:
        if re.search(pattern, text):
            return label
    return None

TOOLS = [
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
                "verified_only": {"type": "boolean"},
                "min_confidence": {"type": "number"},
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
            },
            "required": ["content"],
        },
    },
    {
        "name": "reinforce",
        "description": (
            "Adjust a memory's confidence after observing reality. signal=success when it "
            "held, failure when it did not, correction when you learned the right answer, "
            "decay to fade it. This is how memory stays calibrated instead of trusting stale notes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "signal": {"type": "string", "enum": sorted(db.VALID_SIGNALS)},
                "source": {"type": "string"},
            },
            "required": ["id", "signal"],
        },
    },
    {
        "name": "forget",
        "description": "Delete a memory record that is provably wrong. Prefer reinforce(failure) when merely uncertain.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "stats",
        "description": "Counts of stored memory by type, plus verified totals.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_HANDLERS = {
    "recall": tool_recall,
    "remember": tool_remember,
    "reinforce": tool_reinforce,
    "forget": tool_forget,
    "stats": tool_stats,
}

INSTRUCTIONS = (
    "honcho memory is available. Before non-trivial work, call `recall` with keywords about "
    "the task; prior decisions and corrections are often more reliable than re-deriving them. "
    "After learning something durable (a root cause, a convention, a verified fact), call "
    "`remember` with an honest type and evidence. When a recalled memory is proven right or "
    "wrong in this session, call `reinforce` with success/failure so confidence stays "
    "calibrated. Never store secrets."
)


# --------------------------------------------------------------------------
# JSON-RPC dispatch (shared by both transports)
# --------------------------------------------------------------------------

def _result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_message(msg: Dict[str, Any], user: str = "") -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message. Returns None for notifications."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

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

    def log_message(self, fmt, *args):  # quieter default logging
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
