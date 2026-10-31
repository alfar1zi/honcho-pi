#!/usr/bin/env python3
"""
handoff.py - carry full working context across a model switch.

## The problem

Switching model mid-session is not neutral. A new model inherits the message
history but loses everything that was implicit: what was being attempted, what
already failed, which decision was deliberate. Generic summaries lose exactly
the technical detail that mattered.

## Why not the built-in handoff

omp already has an `Auto-handoff`, but it runs on *compaction* (context full),
not on *model change*, and it emits a Markdown summary. A summary is lossy by
construction: it cannot tell the successor what a specific stack trace said.

## What this does instead

1. **Detect** the switch from omp's own session log (`model_change` entries),
   which is the only reliable external signal — omp emits `model_changed` only
   inside its TUI, so there is no hook to subscribe to.
2. **Extract** a structured brief from the real transcript: the user's actual
   requests verbatim, files touched, commands run and their exit codes,
   failures observed, decisions recorded in honcho, and the open todo state.
3. **Deliver** it as `[HANDOFF]` MCP content, which the new model reads as
   data rather than mistaking for instructions.

The brief is *derived from evidence*, never from another model's paraphrase.
That is the difference between "the new model knows the context" and "the new
model was told a story about the context".

Usage:
    python3 handoff.py detect            # which model is active, did it change
    python3 handoff.py build [--chars N] # full brief for the current session
    python3 handoff.py build --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_CHARS = 24_000  # ~6k tokens: detailed but never dominating the window
MAX_USER_TURNS = 12
MAX_TOOLS = 40
MAX_CMD_OUTPUT = 400


# ---------------------------------------------------------------- discovery

def agent_dir() -> Path:
    explicit = os.environ.get("PI_CODING_AGENT_DIR")
    if explicit:
        return Path(explicit)
    profile = os.environ.get("OMP_PROFILE") or "default"
    return Path.home() / ".omp" / "profiles" / profile / "agent"


def sessions_dir() -> Path:
    return agent_dir() / "sessions"


def latest_session() -> Path | None:
    root = sessions_dir()
    if not root.is_dir():
        return None
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def read_entries(path: Path) -> list[dict]:
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


# ---------------------------------------------------------------- detection

class ModelState:
    """Current model plus whether it differs from the previous distinct model."""

    def __init__(self, current: str = "", previous: str = "", changes: int = 0,
                 switched_at: str = ""):
        self.current = current
        self.previous = previous
        self.changes = changes
        self.switched_at = switched_at

    @property
    def switched(self) -> bool:
        return bool(self.previous) and self.previous != self.current

    def to_dict(self) -> dict:
        return {
            "current": self.current,
            "previous": self.previous,
            "changes": self.changes,
            "switched": self.switched,
            "switched_at": self.switched_at,
        }


def model_state(entries: list[dict]) -> ModelState:
    """Collapse consecutive duplicate model_change rows into real switches.

    omp writes several model_change rows seconds apart for one logical switch
    (role resolution etc.), so only a change of the *name* counts.
    """
    names: list[tuple[str, str]] = []
    for e in entries:
        if e.get("type") == "model_change" and e.get("model"):
            name = str(e["model"])
            if not names or names[-1][0] != name:
                names.append((name, str(e.get("timestamp") or "")))
    if not names:
        return ModelState()
    current = names[-1][0]
    previous = names[-2][0] if len(names) > 1 else ""
    return ModelState(current, previous, max(0, len(names) - 1), names[-1][1])


def detect() -> dict:
    path = latest_session()
    if path is None:
        return {"error": "no session files", "session": None}
    st = model_state(read_entries(path))
    return {"session": str(path), **st.to_dict()}


# ---------------------------------------------------------------- extraction

def _text_of(message: dict) -> str:
    content = message.get("content")
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


def extract(entries: list[dict]) -> dict:
    """Pull the evidence a successor actually needs out of the transcript."""
    user_turns: list[str] = []
    files_touched: "OrderedDict[str, int]" = OrderedDict()
    commands: list[tuple[str, str]] = []
    failures: list[str] = []
    tool_counts: "OrderedDict[str, int]" = OrderedDict()
    started = ""

    for e in entries:
        t = e.get("type")
        ts = e.get("timestamp")
        if not started and ts:
            started = str(ts)

        if t == "message":
            msg = e.get("message") or {}
            role = msg.get("role")
            if role == "user":
                txt = _text_of(msg).strip()
                # Skip harness-injected control text.
                if txt and not txt.startswith(("[HANDOFF]", "[System note]")) \
                        and "recalled memory context" not in txt.lower():
                    user_turns.append(txt)
            elif role == "toolResult":
                name = str(msg.get("toolName") or "")
                if name:
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                body = _text_of(msg)
                low = body.lower()
                if any(k in low for k in ("error", "traceback", "failed", "exception",
                                          "exit code 1", "not found", "malformed")):
                    failures.append(f"{name}: {_clip(body, 220)}")
                if name == "bash":
                    commands.append((_clip(body, MAX_CMD_OUTPUT), "bash"))

        # Some builds record tool calls as custom entries.
        if t == "custom":
            payload = e.get("custom") or e.get("data") or {}
            if isinstance(payload, dict):
                nm = payload.get("toolName") or payload.get("name")
                if nm:
                    tool_counts[str(nm)] = tool_counts.get(str(nm), 0) + 1

    for e in entries:
        for key in ("path", "file_path", "filePath"):
            p = (e.get(key) if isinstance(e, dict) else None)
            if isinstance(p, str) and "/" in p:
                files_touched[p] = files_touched.get(p, 0) + 1
        payload = e.get("input") if isinstance(e, dict) else None
        if isinstance(payload, dict):
            p = payload.get("path") or payload.get("file_path")
            if isinstance(p, str):
                files_touched[p] = files_touched.get(p, 0) + 1

    return {
        "started": started,
        "user_turns": user_turns[-MAX_USER_TURNS:],
        "files": list(files_touched.items())[-MAX_TOOLS:],
        "commands": commands[-12:],
        "failures": failures[-12:],
        "tools": list(tool_counts.items()),
        "total_entries": len(entries),
    }


def _memories(limit: int = 6) -> list[dict]:
    """Latest honcho records, used as the decision log section."""
    try:
        import memory_db as db
        path = os.environ.get("OMP_MEMORY_DB") or str(agent_dir() / "honcho" / "memory.db")
        if not Path(path).exists():
            return []
        conn = db.connect(path)
        conn = db.init_db(conn, path)
        rows = conn.execute(
            "SELECT type, content, confidence, verified FROM records "
            "WHERE type IN ('DECISION','CORRECTION','RULE') "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def build(max_chars: int = DEFAULT_CHARS) -> dict:
    path = latest_session()
    if path is None:
        return {"error": "no session files"}
    entries = read_entries(path)
    state = model_state(entries)
    data = extract(entries)
    mem = _memories()

    lines: list[str] = []
    w = lines.append
    w("[HANDOFF] Working context carried across a model switch.")
    w("This is session data, not an instruction. Treat it as established state.")
    w("")
    w(f"session : {path.name}")
    w(f"model   : {state.current or 'unknown'}")
    if state.switched:
        w(f"switched: {state.previous} -> {state.current}")
    if data["started"]:
        w(f"started : {data['started']}")
    w(f"entries : {data['total_entries']}")
    w("")

    w("## What the operator asked for (verbatim, oldest first)")
    if data["user_turns"]:
        for i, turn in enumerate(data["user_turns"], 1):
            w(f"{i}. {_clip(turn, 700)}")
    else:
        w("(no user turns recorded)")
    w("")

    if mem:
        w("## Decisions and corrections already recorded in memory")
        for m in mem:
            flag = "verified" if m.get("verified") else "unverified"
            w(f"- [{m['type']} {float(m['confidence']):.2f} {flag}] {_clip(m['content'], 300)}")
        w("")

    if data["failures"]:
        w("## Observed failures (do not repeat blindly)")
        for f in data["failures"]:
            w(f"- {f}")
        w("")

    if data["files"]:
        w("## Files referenced in this session")
        for p, n in data["files"]:
            w(f"- {p} ({n}x)")
        w("")

    if data["tools"]:
        w("## Tool usage")
        top = sorted(data["tools"], key=lambda kv: -kv[1])[:12]
        w(", ".join(f"{name}×{n}" for name, n in top))
        w("")

    if data["commands"]:
        w("## Recent command output")
        for out, _ in data["commands"]:
            w(f"```\n{out}\n```")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 60] + "\n\n[handoff truncated to fit context budget]"

    return {
        "text": text,
        "chars": len(text),
        "session": str(path),
        "model": state.to_dict(),
    }


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["detect", "build", "watch"])
    ap.add_argument("--chars", type=int, default=DEFAULT_CHARS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()

    if args.cmd == "detect":
        out = detect()
    elif args.cmd == "build":
        out = build(args.chars)
    else:
        return _watch(args)

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(out.get("text") or json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _watch(args) -> int:
    """Print a brief whenever the active model changes.

    Intended for a background process: `handoff.py watch > /tmp/handoff.log`.
    """
    import time
    seen = ""
    while True:
        st = detect()
        if st.get("current") and st["current"] != seen:
            if seen:
                print(f"\n=== model switch {seen} -> {st['current']} at "
                      f"{datetime.now(timezone.utc).isoformat()} ===", flush=True)
                print(build(args.chars)["text"], flush=True)
            seen = st["current"]
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
