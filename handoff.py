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



# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["detect"])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    out = detect()
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        if out.get("switched"):
            print(f"{out['previous']} -> {out['current']}")
        else:
            print(out.get("current") or "unknown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())