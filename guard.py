#!/usr/bin/env python3
"""
guard.py - keep the context window from silently overflowing.

Two jobs:

  1. `check`   report the budget and warn when a single component balloons.
  2. `compact` archive any `memories/*.md` that has grown past a threshold into
     honcho, leaving a pointer stub. Safe to run on a schedule (cron, systemd
     timer, or a shell prompt hook).

Why a threshold and not a rewrite every time: omp re-reads these files each
session, so the only durable fix is keeping them small. A stub is a few hundred
bytes and cannot grow.

Usage:
    python3 guard.py check
    python3 guard.py compact [--threshold-bytes 20000] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_THRESHOLD = 20_000  # ~5k tokens per file: noticeable but not fatal


def agent_dir() -> Path:
    explicit = os.environ.get("PI_CODING_AGENT_DIR")
    if explicit:
        return Path(explicit)
    profile = os.environ.get("OMP_PROFILE") or "default"
    return Path.home() / ".omp" / "profiles" / profile / "agent"


def oversized(mem_root: Path, threshold: int) -> list[Path]:
    if not mem_root.is_dir():
        return []
    out = []
    for p in mem_root.rglob("*.md"):
        if "memories-archive" in str(p):
            continue
        try:
            if p.stat().st_size > threshold:
                out.append(p)
        except OSError:
            continue
    return sorted(out, key=lambda p: -p.stat().st_size)


def cmd_check() -> int:
    r = subprocess.run([sys.executable, str(HERE / "budget.py")],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def cmd_compact(threshold: int, dry_run: bool) -> int:
    mem_root = agent_dir() / "memories"
    big = oversized(mem_root, threshold)
    if not big:
        print(f"guard: tidak ada file > {threshold} bytes")
        return 0
    print(f"guard: {len(big)} file melebihi {threshold} bytes")
    for p in big:
        print(f"  {p.stat().st_size:>8}  {p}")
    if dry_run:
        print("[dry-run] tidak mengubah apa pun")
        return 0
    args = [sys.executable, str(HERE / "reclaim.py"), "--all"]
    r = subprocess.run(args, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    cmp_ = sub.add_parser("compact")
    cmp_.add_argument("--threshold-bytes", type=int, default=DEFAULT_THRESHOLD)
    cmp_.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.cmd == "check":
        return cmd_check()
    return cmd_compact(args.threshold_bytes, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
