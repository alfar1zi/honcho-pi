#!/usr/bin/env python3
"""
budget.py - measure what actually consumes the context window.

Context bloat is invisible until the model starts truncating silently. This
reports each contributor so pruning is a measurement, not a guess.

Usage:
    python3 budget.py                 # current profile
    python3 budget.py --json          # machine-readable
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CHARS_PER_TOKEN = 4.0  # conservative for mixed prose/code


def _agent_dir() -> Path:
    explicit = os.environ.get("PI_CODING_AGENT_DIR")
    if explicit:
        return Path(explicit)
    profile = os.environ.get("OMP_PROFILE") or "default"
    return Path.home() / ".omp" / "profiles" / profile / "agent"


def _size(p: Path) -> int:
    try:
        return p.stat().st_size if p.exists() else 0
    except OSError:
        return 0


def _mcp_tool_counts(mcp_json: Path) -> dict:
    """Count tools per MCP server by protocol handshake.

    Spawning a server just to count is wasteful, so the result is cached in
    `.mcp-tool-counts.json` and refreshed only when mcp.json changes.
    """
    if not mcp_json.exists():
        return {}
    cache_path = mcp_json.parent / ".mcp-tool-counts.json"
    try:
        cache = json.loads(cache_path.read_text())
        if cache.get("mtime") == mcp_json.stat().st_mtime:
            return cache.get("counts", {})
    except (OSError, json.JSONDecodeError):
        cache = {}

    try:
        cfg = json.loads(mcp_json.read_text()).get("mcpServers", {})
    except (OSError, json.JSONDecodeError):
        return {}

    counts = {}
    for name, spec in cfg.items():
        if spec.get("type", "stdio") != "stdio" or not spec.get("command"):
            counts[name] = -1  # remote; not probed
            continue
        try:
            proc = subprocess.run(
                [spec["command"], *spec.get("args", [])],
                input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                capture_output=True, text=True, timeout=15,
                env={**os.environ, **spec.get("env", {})},
            )
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tools = (msg.get("result") or {}).get("tools")
                if tools is not None:
                    counts[name] = len(tools)
                    break
            counts.setdefault(name, -1)
        except (OSError, subprocess.TimeoutExpired):
            counts[name] = -1

    try:
        cache_path.write_text(json.dumps({"mtime": mcp_json.stat().st_mtime, "counts": counts}))
    except OSError:
        pass
    return counts


def main() -> int:
    agent = _agent_dir()
    as_json = "--json" in sys.argv

    parts: dict[str, int] = {}
    for label, rel in (
        ("SYSTEM_TEMPLATE.md", "SYSTEM_TEMPLATE.md"),
        ("APPEND_SYSTEM.md", "APPEND_SYSTEM.md"),
        ("SYSTEM.md", "SYSTEM.md"),
    ):
        parts[label] = _size(agent / rel)

    memories = agent / "memories"
    mem_bytes = 0
    if memories.is_dir():
        for p in memories.rglob("*.md"):
            mem_bytes += _size(p)
    parts["memories/*.md"] = mem_bytes

    counts = _mcp_tool_counts(agent / "mcp.json")
    total_tools = sum(c for c in counts.values() if c > 0)

    # Tool schemas cost roughly 250-400 tokens each once rendered.
    tool_tokens = total_tools * 300

    text_tokens = sum(parts.values()) / CHARS_PER_TOKEN
    total = text_tokens + tool_tokens

    if as_json:
        print(json.dumps({
            "agent_dir": str(agent),
            "bytes": parts,
            "mcp_tool_counts": counts,
            "text_tokens": round(text_tokens),
            "tool_tokens_est": round(tool_tokens),
            "total_tokens_est": round(total),
        }, indent=2))
        return 0

    print(f"agent dir: {agent}\n")
    print(f"{'component':24} {'bytes':>9} {'~tokens':>9}")
    print("-" * 44)
    for label, b in sorted(parts.items(), key=lambda kv: -kv[1]):
        if b:
            print(f"{label:24} {b:>9} {round(b / CHARS_PER_TOKEN):>9}")
    print("-" * 44)
    if counts:
        print(f"\nMCP servers (tools):")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            shown = "remote" if n < 0 else f"{n} tools"
            print(f"  {name:20} {shown}")
        print(f"  {'TOTAL':20} {total_tools} tools  ~{round(tool_tokens)} tokens")
    print(f"\nTEXT   ~{round(text_tokens):>7} tokens")
    print(f"TOOLS  ~{round(tool_tokens):>7} tokens")
    print(f"TOTAL  ~{round(total):>7} tokens")

    window = int(os.environ.get("HONCHO_CONTEXT_WINDOW", "128000"))
    pct = 100.0 * total / window
    verdict = "OK" if pct < 45 else ("WASPADA" if pct < 70 else "KRITIS")
    print(f"\nwindow {window} -> {pct:.1f}% terpakai [{verdict}]")
    if pct >= 45:
        print("Saran: lipat memories ke honcho (SQLite), matikan MCP yang tak dipakai,")
        print("       atau pindahkan aturan non-aktif keluar dari SYSTEM_TEMPLATE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
