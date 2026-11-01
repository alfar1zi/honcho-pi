#!/usr/bin/env python3
"""
setup.py - register honcho's MCP server with the agents you actually have.

    python3 setup.py detect                       # what is on PATH, and how far honcho can go
    python3 setup.py status                       # table: installed | level | configured
    python3 setup.py install [--agent ID ...] [--all] [--dry-run] [--name N]
    python3 setup.py print --agent ID             # pasteable snippet

Nothing here is agent-specific: every behaviour comes from agent_registry's
table and agent_config's writers. Adding an agent means adding a row.

Exit codes: 0 all requested work succeeded (skipped/would-write counts as
success), 1 at least one failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import agent_config as cfg
import agent_registry as reg

LEVEL_LABEL = {"full": "full", "print-only": "print-only", "none": "none"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - len(text))


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(_pad(h, w) for h, w in zip(headers, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip())


def _yes_no(value: Optional[bool]) -> str:
    if value is None:
        return "?"
    return "yes" if value else "no"


def _configured(agent: reg.Agent, name: Optional[str] = None) -> Optional[bool]:
    """True/False, or None ("?") when the file cannot be read as a config."""
    return cfg.is_configured(agent, name=name)


def _targets(args: argparse.Namespace) -> List[reg.Agent]:
    """Resolve --agent (repeatable, alias-aware) and/or --all."""
    chosen: List[reg.Agent] = []
    for token in args.agent or []:
        for part in token.split(","):
            if not part.strip():
                continue
            try:
                chosen.append(reg.resolve(part.strip()))
            except KeyError as exc:
                raise SystemExit(f"setup.py: {exc.args[0]}")
    if args.all or not chosen:
        chosen = [a for a in reg.all_agents() if a.writable]
    seen = set()
    unique = []
    for agent in chosen:
        if agent.id not in seen:
            seen.add(agent.id)
            unique.append(agent)
    return unique


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_detect(args: argparse.Namespace) -> int:
    runtime = reg.current_runtime()
    installed = cfg.detect_installed(runtime=runtime)
    agents = reg.all_agents()
    print(f"runtime : {runtime}  ({reg_platform()})")
    print(f"registry: {len(agents)} agents  ({reg.ATTRIBUTION})")
    by_level: Dict[str, int] = {}
    for a in agents:
        by_level[a.integration_level] = by_level.get(a.integration_level, 0) + 1
    print("levels  : " + ", ".join(f"{k}={v}" for k, v in sorted(by_level.items())))
    print()
    if not installed:
        print("No registered agent found on PATH.")
        print("Use `python3 setup.py print --agent ID` to get a snippet for any agent.")
        return 0
    rows = []
    for aid in installed:
        agent = reg.get_agent(aid)
        configured = _configured(agent, args.name) if args.configured else None
        note = agent.printed_only_reason or ""
        if cfg.version_warning(agent):
            note = (note + "; " if note else "") + "opencode v2 detected"
        row = [agent.id, agent.binary, agent.mcp_style, agent.integration_level]
        if args.configured:
            row.append(_yes_no(configured))
        row.append(_shorten(note, 44))
        rows.append(row)
    headers = ["agent", "binary", "style", "level"]
    if args.configured:
        headers.append("configured?")
    headers.append("note")
    _print_table(headers, rows)
    print()
    print(f"{len(installed)} installed agent(s) detected, "
          f"{sum(1 for a in installed if reg.get_agent(a).writable)} writable automatically.")
    return 0


def _shorten(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    head = text[:width - 3].rsplit(" ", 1)[0]
    return head + "..."


def reg_platform() -> str:
    import platform
    return platform.platform(terse=True)


def cmd_status(args: argparse.Namespace) -> int:
    rows = []
    for agent in reg.all_agents():
        installed = agent.is_installed(runtime=reg.current_runtime())
        configured = _configured(agent, args.name)
        path = cfg.choose_path(agent)
        rows.append([
            agent.id,
            _yes_no(installed),
            agent.integration_level,
            _yes_no(configured),
            path or "-",
        ])
    _print_table(["agent", "installed", "level", "configured", "config path"], rows)
    full = reg.ids_by_level("full")
    print()
    print(f"{len(full)} agent(s) can be configured automatically; "
          f"{len(reg.ids_by_level('print-only'))} print-only; "
          f"{len(reg.ids_by_level('none'))} without MCP.")
    return 0


def _verify_unresolved(agent: reg.Agent, spec: Dict[str, object]) -> Optional[str]:
    """Exercise the writer for an agent whose real path cannot be resolved.

    `--all --dry-run` must show every writable agent actually working, not
    skip the ones whose env var is unset on this machine. The write happens in
    a throwaway directory (from a copy of the real file when one exists, else
    a stub in the agent's own format) and the temp dir is removed after.
    """
    import tempfile
    with tempfile.TemporaryDirectory(prefix="honcho-verify-") as tmp:
        real = agent.config_candidates() or [None]
        target = os.path.join(tmp, "probe-" + (real[0] or "config").rsplit("/", 1)[-1])
        if real[0] and os.path.exists(real[0]):
            shutil.copyfile(real[0], target)
        # No real file: leave the probe missing. A synthetic empty file would
        # be refused by design ("file exists but is empty"), which is not what
        # the user would hit on a fresh install.
        probe = cfg.Agent.__new__(cfg.Agent)
        object.__setattr__(probe, "__dict__", {**agent.__dict__, "config_path": target})
        result = cfg.write_entry(probe, env={}, spec=spec)
        return None if result.ok else f"writer would fail: {result.message}"


def cmd_install(args: argparse.Namespace) -> int:
    targets = _targets(args)
    if not targets:
        print("Nothing to install.")
        return 0
    spec = cfg.render_server_spec(name=args.name)
    failures = 0
    changed = 0
    for agent in targets:
        warning = cfg.version_warning(agent)
        # install() isolates every agent: an unexpected exception becomes this
        # agent's error, so one broken agent never aborts the run.
        result = cfg.install(agent, dry_run=args.dry_run, spec=spec)
        if (args.dry_run and args.all and result.status == cfg.STATUS_SKIPPED
                and agent.writable and not agent.config_candidates()):
            problem = _verify_unresolved(agent, spec)
            if problem:
                result = cfg.Result(agent.id, cfg.STATUS_ERROR, problem,
                                    requested=agent.config_path)
            else:
                print(f"[dry-run] {agent.id}: would write {agent.config_path} "
                      f"(path unresolved here; plan verified in a temp copy)")
                changed += 1
                continue
        prefix = "[dry-run] " if args.dry_run else ""
        print(f"{prefix}{result}")
        if warning:
            print(f"  warning: {warning}")
            if agent.integration_level == "full" and result.status == cfg.STATUS_OK:
                print("  the entry was written in v1 schema as reported above")
        if result.status in (cfg.STATUS_OK, cfg.STATUS_DRY_RUN):
            changed += 1
        if result.status == cfg.STATUS_ERROR:
            failures += 1
            if args.verbose and result.traceback:
                print(_indent(result.traceback))
        if args.verbose and result.diff and result.status in (cfg.STATUS_OK,
                                                             cfg.STATUS_DRY_RUN):
            print(_indent(result.diff))
        if (args.verbose and result.snippet
                and result.status in (cfg.STATUS_SKIPPED, cfg.STATUS_ERROR)):
            print(_indent(result.snippet))
    print()
    verb = "would change" if args.dry_run else "changed"
    print(f"{changed} {verb}, {failures} failed, {len(targets)} considered.")
    if changed and not args.dry_run:
        print("Restart the agent(s) to pick up the server: "
              "recall, remember, reinforce, forget, stats.")
    return 1 if failures else 0


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.rstrip("\n").splitlines())


def cmd_print(args: argparse.Namespace) -> int:
    spec = cfg.render_server_spec(name=args.name)
    for token in args.agent or []:
        try:
            agent = reg.resolve(token)
        except KeyError as exc:
            print(f"setup.py: {exc.args[0]}", file=sys.stderr)
            return 2
        path = cfg.choose_path(agent)
        print(f"# {agent.id}  (style: {agent.mcp_style}, level: "
              f"{agent.integration_level})")
        if path:
            print(f"# file: {path}")
        else:
            print("# file: unknown for this environment")
        if agent.note:
            print(f"# note: {agent.note}")
        warning = cfg.version_warning(agent)
        if warning:
            print(f"# warning: {warning}")
        print(cfg.snippet_for(agent, spec))
        print()
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="Register honcho with any MCP-capable agent, from one data registry.",
    )
    sub = parser.add_subparsers(dest="command")

    d = sub.add_parser("detect", help="list agents found on PATH and how honcho can configure them")
    d.add_argument("--name", default=cfg.SERVER_NAME, help="server name to look for")
    d.add_argument("--configured", action="store_true",
                   help="also show whether each detected agent already has honcho")
    d.set_defaults(func=cmd_detect)

    s = sub.add_parser("status", help="table of every agent: installed, level, configured")
    s.add_argument("--name", default=cfg.SERVER_NAME, help="server name to look for")
    s.set_defaults(func=cmd_status)

    i = sub.add_parser("install", help="write the honcho entry into agent configs")
    i.add_argument("--agent", action="append", metavar="ID",
                   help="agent id or alias (repeatable, comma-separated ok)")
    i.add_argument("--all", action="store_true",
                   help="every agent honcho can write (the default when --agent is absent)")
    i.add_argument("--dry-run", action="store_true",
                   help="show what would change; never touches a file")
    i.add_argument("--name", default=cfg.SERVER_NAME, help="server name to register")
    i.add_argument("-v", "--verbose", action="store_true", help="show diffs and snippets")
    i.set_defaults(func=cmd_install)

    p = sub.add_parser("print", help="print a pasteable snippet for an agent")
    p.add_argument("--agent", action="append", required=True, metavar="ID")
    p.add_argument("--name", default=cfg.SERVER_NAME)
    p.set_defaults(func=cmd_print)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # e.g. `setup.py status | head`
        try:
            sys.stdout.close()
        except OSError:
            pass
        sys.exit(0)
