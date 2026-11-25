#!/usr/bin/env python3
"""Command line interface: one entry point for capture, search, recap, health.

The shell scripts in bin/ are thin shims so cron, hooks, and manual use keep
working without duplicating Python heredocs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .capture import scan_text
from .context import build_memory_context_block

DEFAULT_MIN_FREE_MB = 100


def _memory_dir() -> Path:
    return Path(db.DB_PATH).parent


def _sessions_dir() -> Path:
    return _memory_dir() / "sessions"


class CliError(Exception):
    """A user-facing failure: report it plainly, exit non-zero, no traceback."""


def _open():
    """Open and migrate the store, turning storage failures into a clear
    message. Without this a corrupt or unreadable database surfaced as a
    bare traceback on stderr and an empty stdout, which reads as success to
    anything that only checks stdout."""
    try:
        conn = db.connect()
    except Exception as exc:  # sqlite3.OperationalError, OSError, ...
        raise CliError(f"cannot open the memory database: {exc}") from exc
    try:
        db.init_db(conn)
    except Exception as exc:
        conn.close()
        raise CliError(f"cannot initialise the memory database: {exc}") from exc
    return conn


# --- secret scanning ---------------------------------------------------------
# Patterns and scan_text live in capture.py so the auto-capture pipeline and
# this CLI guard share one definition.


def cmd_scan(args) -> int:
    text = args.text
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    findings = scan_text(text)
    for kind, fragment in findings:
        print(f"WARNING: possible {kind} detected: {fragment}...", file=sys.stderr)
    return 1 if findings else 0


# --- capture -----------------------------------------------------------------

def _project_id() -> str:
    """Stable short id for the current git project, or '' outside a repo."""
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if remote.returncode == 0 and remote.stdout.strip():
        return hashlib.sha256(remote.stdout.strip().encode()).hexdigest()[:12]
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if top.returncode == 0 and top.stdout.strip():
        return hashlib.sha256(top.stdout.strip().encode()).hexdigest()[:12]
    return ""


def _check_disk(min_free_mb: int) -> bool:
    target = _memory_dir()
    target.mkdir(parents=True, exist_ok=True)
    try:
        usage = shutil.disk_usage(str(target))
    except OSError:
        return False
    return usage.free >= min_free_mb * 1024 * 1024


def cmd_capture(args) -> int:
    if not args.task or not args.outcome:
        print("error: --task and --outcome are required", file=sys.stderr)
        return 2
    if args.type not in db.VALID_TYPES:
        print(f"error: invalid --type {args.type}", file=sys.stderr)
        return 2

    combined = f"{args.task} {args.outcome} {args.evidence}"
    findings = scan_text(combined)
    if findings and not args.allow_secrets:
        for kind, fragment in findings:
            print(f"error: possible {kind} in capture input: {fragment}...", file=sys.stderr)
        print("refusing to store; pass --allow-secrets to override", file=sys.stderr)
        return 1

    if not _check_disk(args.min_free_mb):
        print(f"error: less than {args.min_free_mb}MB free in {_memory_dir()}", file=sys.stderr)
        return 1

    project = args.project or _project_id()
    session = args.session or f"sess-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    content = f"{args.task} -> {args.outcome}"

    conn = _open()
    try:
        db.start_session(conn, session_id=session, project_id=project,
                         parent_id=args.parent_id)
        existing = db.find_record(conn, args.type, content, args.source, project)
        record_id = db.upsert_record(
            conn,
            rtype=args.type,
            content=content,
            evidence=args.evidence,
            verified=1 if args.verified else 0,
            confidence=args.confidence,
            source=args.source,
            tags=args.tags,
            session_id=session,
            project_id=project,
            parent_id=args.parent_id,
        )
        if existing and existing["id"] == record_id:
            print(f"updated {record_id} (type={args.type})")
        else:
            print(f"captured {record_id} (type={args.type}, confidence={args.confidence})")
    finally:
        conn.close()

    _append_jsonl(session, project, args, content)
    if not args.no_audit:
        _audit("capture", content, session, project)
    return 0


def _append_jsonl(session: str, project: str, args, content: str) -> None:
    """Durable append-only capture log; survives a DB failure."""
    sessions = _sessions_dir()
    sessions.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = sessions / f"capture-{stamp}-{os.getpid()}.jsonl"
    entry = {
        "timestamp": db.now_iso(),
        "session": session,
        "project": project,
        "task": args.task,
        "outcome": args.outcome,
        "type": args.type,
        "evidence": args.evidence,
        "verified": int(bool(args.verified)),
        "confidence": args.confidence,
        "source": args.source,
        "tags": args.tags,
        "parent_id": args.parent_id,
    }
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _audit(action: str, detail: str, session: str, project: str) -> None:
    log = _memory_dir() / "audit.log"
    line = (
        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] "
        f"action={action} session={session} project={project} detail={detail}\n"
    )
    try:
        with log.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except OSError:
        pass


# --- search / list / recap ---------------------------------------------------

def cmd_search(args) -> int:
    conn = _open()
    try:
        rows = db.search(
            conn, args.query, limit=args.limit, rtype=args.type,
            project_id=args.project, verified_only=args.verified,
            min_confidence=args.min_confidence,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("no matches")
        return 0
    for row in rows:
        print(f"{row['id']}  [{row['type']}]  conf={row['confidence']:.2f}  {row['content']}")
        if row.get("evidence"):
            print(f"    evidence: {row['evidence']}")
    return 0


def cmd_recap(args) -> int:
    conn = _open()
    try:
        if args.session:
            rows = [r for r in db.list_records(conn, limit=10_000)
                    if r["session_id"] == args.session]
        else:
            rows = db.list_records(conn, project_id=args.project, limit=args.limit)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    header = args.session or args.project or "all records"
    print(f"# {header}")
    print(f"{len(rows)} records")
    for row in rows:
        mark = "verified" if row["verified"] else "unverified"
        print(f"- [{row['type']}/{mark} conf={row['confidence']:.2f}] {row['content']}")
        if row.get("evidence"):
            print(f"  evidence: {row['evidence']}")
    return 0


def cmd_context(args) -> int:
    """Emit a fenced memory-context block for prompt injection."""
    conn = _open()
    try:
        rows = db.search(conn, args.query, limit=args.limit, project_id=args.project)
    finally:
        conn.close()
    body = "\n".join(f"- [{r['type']}] {r['content']}" for r in rows)
    block = build_memory_context_block(body)
    if not block:
        return 0
    print(block, end="")
    return 0


# --- reinforce / promote -----------------------------------------------------

def cmd_reinforce(args) -> int:
    conn = _open()
    try:
        new_conf = db.reinforce(conn, args.record_id, args.signal,
                                content=getattr(args, "content", None),
                                evidence=getattr(args, "evidence", None),
                                delta=args.delta, source=args.source)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"{args.record_id} {args.signal} -> confidence {new_conf:.2f}")
    return 0


def cmd_maintenance(args) -> int:
    conn = _open()
    try:
        if args.op == "decay":
            print(json.dumps({"decayed": db.apply_decay(conn)}, indent=2, sort_keys=True))
        elif args.op == "forget_cold":
            print(json.dumps(db.forget_cold(conn, apply=args.apply), indent=2, sort_keys=True))
        elif args.op == "promote":
            print(json.dumps({"promotion": db.promote_hot(conn)}, indent=2, sort_keys=True))
    finally:
        conn.close()
    return 0


def cmd_promote(args) -> int:
    """Promote verified, confident inferences/facts to RULE records."""
    conn = _open()
    promoted = 0
    try:
        for row in db.list_records(conn, limit=10_000, project_id=args.project):
            if row["type"] not in ("INFERENCE", "FACT"):
                continue
            if not row["verified"] or row["confidence"] < args.threshold:
                continue
            existing = db.find_record(conn, "RULE", row["content"], f"promote:{args.source}",
                                      row["project_id"])
            if existing:
                continue
            db.upsert_record(
                conn, rtype="RULE", content=row["content"], evidence=row["evidence"],
                verified=1, confidence=row["confidence"], source=f"promote:{args.source}",
                tags=row["tags"], session_id=row["session_id"],
                project_id=row["project_id"], parent_id=row["id"],
            )
            promoted += 1
            print(f"promoted {row['id']} -> RULE: {row['content'][:80]}")
    finally:
        conn.close()
    print(f"{promoted} record(s) promoted (threshold={args.threshold})")
    return 0


# --- analyze ----------------------------------------------------------------

def cmd_analyze(args) -> int:
    """Detect recurring keywords across capture logs and record INFERENCE rows."""
    sessions = _sessions_dir()
    captures = []
    if sessions.is_dir():
        for path in sorted(sessions.glob("*.jsonl")):
            try:
                with path.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            captures.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

    stop = {
        "the", "and", "for", "with", "from", "that", "this", "have", "has", "had",
        "was", "were", "been", "will", "would", "could", "should", "may", "might",
        "must", "can", "just", "also", "into", "onto", "over", "under", "after",
        "before", "between", "through", "during", "without", "within", "about",
        "against", "among", "upon", "toward", "towards", "across", "along",
        "around", "behind", "below", "beneath", "beside", "besides", "per", "via",
        "not", "yes", "okay",
    }

    import re

    groups = {}
    for cap in captures:
        text = f"{cap.get('task', '')} {cap.get('outcome', '')}".lower()
        for word in set(re.findall(r"[a-z0-9]{3,}", text)):
            if word in stop:
                continue
            groups.setdefault(word, []).append(cap)

    findings = []
    for word, items in groups.items():
        if len(items) < args.min_occurrences:
            continue
        findings.append({
            "content": f"Recurring pattern '{word}': {len(items)} occurrences across sessions",
            "evidence": "; ".join(f"{i.get('task', '')} -> {i.get('outcome', '')}" for i in items[:5]),
            "confidence": min(0.5 + len(items) * 0.08, 0.95),
            "tags": f"pattern,{word}",
        })

    for cap in captures:
        if cap.get("type") == "CORRECTION":
            findings.append({
                "content": f"User correction: {cap.get('task', '')} -> {cap.get('outcome', '')}",
                "evidence": cap.get("evidence") or "user correction",
                "confidence": 0.8,
                "tags": "correction,user-feedback",
            })

    conn = _open()
    written = 0
    try:
        for finding in findings:
            source = "analyze-session"
            if db.find_record(conn, "INFERENCE", finding["content"], source, args.project or ""):
                continue
            db.upsert_record(
                conn, rtype="INFERENCE", content=finding["content"],
                evidence=finding["evidence"], verified=0,
                confidence=finding["confidence"], source=source,
                tags=finding["tags"], session_id=args.session or "",
                project_id=args.project or "",
            )
            written += 1
        total = db.stats(conn)["total"]
    finally:
        conn.close()

    print(f"analyzed {len(captures)} captures, {len(groups)} keyword groups, {total} records")
    print(f"wrote {written} new INFERENCE record(s)")
    return 0


# --- ops: health / backup / repair / stats / disk ---------------------------

def cmd_health(args) -> int:
    conn = _open()
    try:
        report = db.health(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"status:  {report['status']}")
        print(f"db:      {report['db_path']}")
        print(f"records: {report['total_records']}")
        print(f"backups: {report['backup_count']}")
    return 0 if report["status"] == "ok" else 1


def cmd_backup(args) -> int:
    path = db.backup_db(db.DB_PATH)
    if not path:
        print("backup failed: database missing or unreadable", file=sys.stderr)
        return 1
    print(f"backup: {path}")


def cmd_mirror(args) -> int:
    out_dir = Path(args.dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = _open()
    try:
        exported = db.export_markdown(conn, out_dir)
    finally:
        conn.close()
    synced = db.sync_mirror(out_dir, message=args.message, init=args.init)
    parts = [f"written={len(exported['written'])}", f"removed={len(exported['removed'])}",
             f"unchanged={len(exported['unchanged'])}"]
    if synced.get("committed"):
        parts.append(f"committed={synced['sha'][:12]}")
    else:
        parts.append(f"git={synced.get('reason', 'clean')}")
    print("mirror: " + ", ".join(parts))
    return 0

def cmd_skill_export(args) -> int:
    conn = _open()
    try:
        result = db.export_skill(conn, args.record_id, args.dir)
    except (KeyError, ValueError) as e:
        print(f"skill-export: {e}")
        return 1
    finally:
        conn.close()
    print(f"skill-export: {result['dir']} ({len(result['references'])} references)")
    return 0


def cmd_pack(args) -> int:
    conn = _open()
    try:
        result = db.export_pack(conn, args.file)
    finally:
        conn.close()
    print(f"pack: {result['records']} records -> {result['file']} "
          f"({result['bytes']} bytes, sha256 {result['sha256'][:12]})")
    return 0


def cmd_unpack(args) -> int:
    conn = _open()
    try:
        result = db.import_pack(conn, args.file)
    except (ValueError, OSError) as e:
        print(f"unpack: {e}")
        return 1
    finally:
        conn.close()
    print(f"unpack: {result['imported']} new, {result['merged']} merged")
    return 0


def cmd_repair(args) -> int:
    ok = db.repair_from_backup(db.DB_PATH)
    print("repair: ok" if ok else "repair: failed, no usable backup")
    return 0 if ok else 1


def cmd_stats(args) -> int:
    conn = _open()
    try:
        data = db.stats(conn)
    finally:
        conn.close()
    print(json.dumps(data, indent=2))
    return 0

def cmd_digest(args) -> int:
    conn = _open()
    try:
        d = db.digest(conn, budget=args.budget, project_id=args.project)
    finally:
        conn.close()
    print(d["text"])
    return 0


def cmd_disk(args) -> int:
    ok = _check_disk(args.min_free_mb)
    try:
        usage = shutil.disk_usage(str(_memory_dir()))
        free_mb = usage.free // (1024 * 1024)
    except OSError:
        free_mb = 0
    if args.json:
        print(json.dumps({"available_mb": free_mb, "min_required_mb": args.min_free_mb, "ok": ok}))
    else:
        print(f"{'ok' if ok else 'warning'}: {free_mb}MB free (need {args.min_free_mb}MB)")
    return 0 if ok else 1


def cmd_checkpoint(args) -> int:
    conn = _open()
    try:
        cp = db.create_pre_compress_checkpoint(
            conn, session_id=args.session, project_id=args.project or "",
            evidence_hash=args.evidence_hash or "",
        )
    finally:
        conn.close()
    print(json.dumps(cp, indent=2))
    return 0


def cmd_restore(args) -> int:
    conn = _open()
    try:
        result = db.restore_from_checkpoint(conn, args.checkpoint_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0


# --- parser -----------------------------------------------------------------


def cmd_overview(args) -> int:
    from . import web

    return web.main(["--port", str(args.port)] + (["--no-open"] if args.no_open else []))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omp-memory",
        description="Local-first SQLite memory for coding agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("capture", help="record a task -> outcome lesson")
    p.add_argument("--task", required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--type", default="FACT", choices=sorted(db.VALID_TYPES))
    p.add_argument("--evidence", default="")
    p.add_argument("--verified", action="store_true")
    p.add_argument("--confidence", type=float, default=0.5)
    p.add_argument("--source", default="session-capture")
    p.add_argument("--tags", default="")
    p.add_argument("--project", default="")
    p.add_argument("--session", default="")
    p.add_argument("--parent-id", default="")
    p.add_argument("--min-free-mb", type=int, default=DEFAULT_MIN_FREE_MB)
    p.add_argument("--allow-secrets", action="store_true")
    p.add_argument("--no-audit", action="store_true")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("search", help="full-text search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--type", default=None, choices=sorted(db.VALID_TYPES))
    p.add_argument("--project", default=None)
    p.add_argument("--verified", action="store_true")
    p.add_argument("--min-confidence", type=float, default=0.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("recap", help="summarise a session or project")
    p.add_argument("--session", default=None)
    p.add_argument("--project", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recap)

    p = sub.add_parser("context", help="emit a fenced memory-context block")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("reinforce", help="apply a reinforcement signal")
    p.add_argument("record_id")
    p.add_argument("signal", choices=sorted(db.VALID_SIGNALS))
    p.add_argument("--delta", type=float, default=None)
    p.add_argument("--source", default="")
    p.add_argument("--content", default=None,
                   help="correction only: the corrected statement")
    p.add_argument("--evidence", default=None,
                   help="why the correction is true")
    p.set_defaults(func=cmd_reinforce)

    p = sub.add_parser("promote", help="promote verified records to RULE")
    p.add_argument("--threshold", type=float, default=0.85)
    p.add_argument("--project", default=None)
    p.add_argument("--source", default="promote")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("analyze", help="derive INFERENCE rows from capture logs")
    p.add_argument("--project", default=None)
    p.add_argument("--session", default=None)
    p.add_argument("--min-occurrences", type=int, default=2)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("health", help="health report")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("stats", help="record counts by type")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("digest", help="always-in-context memory block (char budget)")
    p.add_argument("--budget", type=int, default=2000)
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("maintenance", help="decay, forget cold records, promote hot ones")
    p.add_argument("op", choices=["decay", "forget_cold", "promote"])
    p.add_argument("--apply", action="store_true",
                   help="forget_cold: actually delete (default report only)")
    p.set_defaults(func=cmd_maintenance)
    p = sub.add_parser("backup", help="consistent snapshot")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("repair", help="restore from newest usable backup")
    p.set_defaults(func=cmd_repair)
    p = sub.add_parser("mirror", help="export records to markdown and commit to a git mirror")
    p.add_argument("dir", help="mirror directory (created if missing)")
    p.add_argument("-m", "--message", default=None, help="git commit message")
    p.add_argument("--init", action="store_true",
                   help="git init the directory when it is not a repository yet")
    p.set_defaults(func=cmd_mirror)

    p = sub.add_parser("skill-export", help="export a procedural record as an agentskills.io SKILL.md")
    p.add_argument("record_id")
    p.add_argument("dir", help="output directory (skill folder created inside)")
    p.set_defaults(func=cmd_skill_export)

    p = sub.add_parser("pack", help="export the store as a compressed sync pack")
    p.add_argument("file", help="output .pack file (committable to a team repo)")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("unpack", help="import a sync pack (content-identity merge)")
    p.add_argument("file", help="pack file to import")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("disk", help="check free space")
    p.add_argument("min_free_mb", type=int, nargs="?", default=DEFAULT_MIN_FREE_MB)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_disk)

    p = sub.add_parser("scan", help="scan text or stdin for secrets")
    p.add_argument("text", nargs="?", default="")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("overview", help="open the interactive graph view in a browser")
    p.add_argument("--port", type=int, default=4173)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_overview)

    p = sub.add_parser("checkpoint", help="snapshot session evidence before compression")
    p.add_argument("--session", required=True)
    p.add_argument("--project", default="")
    p.add_argument("--evidence-hash", default="")
    p.set_defaults(func=cmd_checkpoint)

    p = sub.add_parser("restore", help="restore a pre-compress checkpoint")
    p.add_argument("checkpoint_id")
    p.set_defaults(func=cmd_restore)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
