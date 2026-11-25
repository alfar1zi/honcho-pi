"""Working-context handoff: build a brief a successor model can act on.

Why this lives in the repository: the first version of this file existed only
as a script outside the repo, imported by a test that skipped whenever the
machine lacked it -- so CI never exercised it, and it imported a pre-v18 fork
of the engine. The logic is the same; the placement is what changed.

Contract:
  * The brief is assembled from transcript evidence (verbatim operator turns,
    recorded decisions, observed failures, files), never from a model's
    paraphrase.
  * Truncation drops whole low-priority sections instead of slicing the tail,
    so a fenced block is never half-written.
  * `build()` returns `usable`, and the CLI exits 2 when a small budget left
    no room for content. A caller must skip injection rather than inject a
    header with no body.
  * `model_state()` counts only `role == "default"` switches: omp also records
    retry-fallback hops and prewalk promotions as model_change rows, and those
    are the harness moving traffic, not the operator choosing a model.

The harness-specific parts (session path layout, entry shapes) are the only
things OMP-shaped here; everything else is plain JSON handling.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path


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
    # Role matters. omp records retry-fallback hops (role="fallback") and
    # prewalk/prompt-tier promotions (role="prewalk"/"temporary") as
    # model_change rows too. Those are the harness moving traffic inside one
    # operator turn, not the operator choosing a different model. Counting
    # them overstated this session as 18 switches when the log holds 14 real
    # ones (verified: 25 rows -> 14 default + 11 temporary). omp's own
    # lastModelChangeRole(skipEphemeral) skips the same roles.
    names: list[tuple[str, str]] = []
    for e in entries:
        if e.get("type") != "model_change" or not e.get("model"):
            continue
        if str(e.get("role") or "default") != "default":
            continue
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


def _memory_module():
    """This module ships with the engine, so it is always the engine."""
    from . import db as engine_db  # noqa: PLC0415
    return engine_db, "omp_memory"


def _memories(limit: int = 6) -> list[dict]:
    """Latest honcho records, used as the decision log section."""
    try:
        db, _engine = _memory_module()
        path = os.environ.get("OMP_MEMORY_DB") or str(agent_dir() / "honcho" / "memory.db")
        if not Path(path).exists():
            return []
        conn = db.connect(path)
        # init_db returns None (it migrates in place); assigning its result
        # used to replace the connection with None and the whole section was
        # silently dropped by the except below.
        db.init_db(conn, path)
        rows = conn.execute(
            "SELECT type, content, confidence, verified FROM records "
            "WHERE type IN ('DECISION','CORRECTION','RULE') "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        # Returning [] silently hid a real failure (the fixture's DB was not
        # the one being read). Keep the brief working, but leave a trace that
        # OMP_HANDOFF_DEBUG surfaces instead of guessing later.
        if os.environ.get("OMP_HANDOFF_DEBUG"):
            print(f"handoff: memory section skipped: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        return []


# Section priority when the brief does not fit. Order is deliberate: the
# operator's own words and the recorded decisions change what the successor
# does; tool counts and raw command output only add flavour. The old code cut
# the tail blindly, so the section it always destroyed first was "Recent
# command output" -- which is also the only one that can truncate mid
# code-fence and produce malformed text.
_SECTION_PRIORITY = (
    "## What the operator asked for",
    "## Decisions and corrections",
    "## Observed failures",
    "## Files referenced",
    "## Tool usage",
    "## Recent command output",
)


def _split_sections(lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split the brief into its preamble and its `## ` sections, in order."""
    head: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith("## "):
            sections.append((line, [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            head.append(line)
    return head, sections


def _trim_to_budget(lines: list[str], max_chars: int) -> tuple[str, bool]:
    """Fit the brief into max_chars by dropping whole low-priority sections.

    Never slices mid-line, so a fenced block is either present or absent, not
    half-written. The header (session, model, switch, entry count) is always
    kept: without it the block is unreadable.
    """
    head, sections = _split_sections(lines)
    order = {name: i for i, name in enumerate(_SECTION_PRIORITY)}

    def rank(item: tuple[str, list[str]]) -> int:
        title = item[0]
        for name in _SECTION_PRIORITY:
            if title.startswith(name):
                return order[name]
        return len(_SECTION_PRIORITY)  # unknown sections go last

    kept = sorted(sections, key=rank)
    head_text = "\n".join(head).rstrip()
    while True:
        # Check emptiness FIRST. After the last section is popped the joined
        # text trivially fits the budget, so the size check would return a
        # header-only "brief" as if it succeeded -- which is exactly how the
        # earlier version reported usable=True for 411 characters of header.
        if not kept:
            return (head_text + "\n[handoff has no room for content at this "
                    "budget; increase --chars or skip injection]", False)
        text = "\n".join(head + [ln for _, body in kept for ln in body])
        if len(text) <= max_chars:
            dropped = len(sections) - len(kept)
            if dropped:
                text += (f"\n\n[{dropped} lower-priority section(s) omitted to fit "
                         f"the context budget]")
            return text, True
        kept.pop()  # drop the least important remaining section


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
    usable = True
    if len(text) > max_chars:
        text, usable = _trim_to_budget(lines, max_chars)

    return {
        "text": text,
        "chars": len(text),
        # Machine-readable verdict so the caller can skip injection instead of
        # parsing prose. A brief whose body is empty must not be injected.
        "usable": bool(text) and usable,
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
    # Exit 2 = built but not usable (no room for content). A caller that only
    # checks exit status then skips injection instead of injecting a header.
    if out.get("usable") is False:
        return 2
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
