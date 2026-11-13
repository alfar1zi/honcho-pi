"""End-to-end CLI tests. Each covers a defect the old shell scripts had."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, "-m", "omp_memory"]


@pytest.fixture()
def env(tmp_path):
    e = dict(os.environ)
    e["PYTHONPATH"] = str(REPO_ROOT / "src")
    e["OMP_MEMORY_DB"] = str(tmp_path / "memory" / "memory.db")
    e["OMP_MEMORY_BACKUP_DIR"] = str(tmp_path / "memory" / "backups")
    e["HOME"] = str(tmp_path / "home")
    return e


def run(env, *args, stdin=None):
    return subprocess.run(CLI + list(args), capture_output=True, text=True,
                          env=env, input=stdin, timeout=60)


# --- defect: capture aborted because the old script passed a non-existent
#     helper path, and used $1 as the DB path while passing 1 arg -----------

def test_capture_then_search_round_trip(env):
    r = run(env, "capture", "--task", "fix wal mode", "--outcome", "no corruption",
            "--type", "FACT", "--confidence", "0.8")
    assert r.returncode == 0, r.stderr
    assert "captured" in r.stdout

    r = run(env, "search", "wal", "--json")
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert len(rows) == 1
    assert rows[0]["content"] == "fix wal mode -> no corruption"
    assert rows[0]["confidence"] == 0.8


def test_capture_requires_task_and_outcome(env):
    r = run(env, "capture", "--task", "only task")
    assert r.returncode == 2
    assert "outcome" in r.stderr


def test_capture_rejects_unknown_type(env):
    r = run(env, "capture", "--task", "a", "--outcome", "b", "--type", "NOPE")
    assert r.returncode != 0


def test_capture_is_idempotent(env):
    args = ("capture", "--task", "same task", "--outcome", "same outcome")
    first = run(env, *args)
    second = run(env, *args)
    assert first.returncode == second.returncode == 0
    assert "updated" in second.stdout, "second identical capture must update, not duplicate"

    rows = json.loads(run(env, "search", "same", "--json").stdout)
    assert len(rows) == 1, f"duplicate rows created: {len(rows)}"


def test_capture_writes_jsonl_log(env):
    r = run(env, "capture", "--task", "log probe", "--outcome", "logged",
            "--session", "sess-1")
    assert r.returncode == 0, r.stderr
    logs = list((Path(env["OMP_MEMORY_DB"]).parent / "sessions").glob("*.jsonl"))
    assert logs, "no capture log written"
    entry = json.loads(logs[0].read_text().strip())
    assert entry["session"] == "sess-1"
    assert entry["task"] == "log probe"


def test_capture_writes_audit_line(env):
    run(env, "capture", "--task", "audit probe", "--outcome", "audited")
    audit = Path(env["OMP_MEMORY_DB"]).parent / "audit.log"
    assert audit.exists()
    assert "action=capture" in audit.read_text()


# --- defect: secret scanning leaked the payload through argv and always
#     aborted because it checked stdout instead of the exit code ------------

def test_capture_blocks_secret_looking_input(env):
    r = run(env, "capture", "--task", "key", "--outcome", "sk-abcdefghijklmnop")
    assert r.returncode == 1
    assert "possible" in r.stderr
    rows = json.loads(run(env, "search", "abcdefghijklmnop", "--json").stdout)
    assert rows == [], "secret-looking input was stored"


def test_capture_allows_override(env):
    r = run(env, "capture", "--task", "key", "--outcome", "sk-abcdefghijklmnop",
            "--allow-secrets")
    assert r.returncode == 0, r.stderr


def test_scan_reads_stdin(env):
    r = run(env, "scan", stdin="token=abcdef1234567890")
    assert r.returncode == 1
    assert "token" in r.stderr


def test_scan_clean_text_passes(env):
    r = run(env, "scan", "a normal sentence about sqlite")
    assert r.returncode == 0


def test_capture_registers_session_with_lineage(env):
    run(env, "capture", "--task", "parent", "--outcome", "root", "--session", "p-1")
    run(env, "capture", "--task", "child", "--outcome", "branch",
        "--session", "c-1", "--parent-id", "p-1")

    r = run(env, "stats")
    assert json.loads(r.stdout)["sessions"] == 2

    r = subprocess.run(
        [sys.executable, "-c",
         "import json,sys; from omp_memory import db;"
         "c=db.connect(); rows={s['id']:s for s in db.list_sessions(c)};"
         "print(json.dumps({k:v['parent_id'] for k,v in rows.items()}))"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    lineage = json.loads(r.stdout)
    assert lineage["c-1"] == "p-1", f"parent lineage missing: {lineage}"


# --- defect: analyze-session read sys.argv[3] five times, so every mode
#     crashed or used the wrong value ---------------------------------------

def test_stats_runs(env):
    run(env, "capture", "--task", "t", "--outcome", "o")
    r = run(env, "stats")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["total"] == 1
    assert data["FACT"] == 1


def test_reinforce_updates_confidence(env):
    run(env, "capture", "--task", "t", "--outcome", "o", "--confidence", "0.5")
    rid = json.loads(run(env, "search", "t", "--json").stdout)[0]["id"]

    r = run(env, "reinforce", rid, "success")
    assert r.returncode == 0, r.stderr
    assert "0.60" in r.stdout

    rows = json.loads(run(env, "search", "t", "--json").stdout)
    assert rows[0]["confidence"] == pytest.approx(0.6)


def test_reinforce_correction_rewrite(env):
    run(env, "capture", "--task", "deploy host is prod-01 in us-east",
        "--outcome", "deploy host is prod-01 in us-east", "--confidence", "0.5")
    rid = json.loads(
        run(env, "search", "deploy host", "--json").stdout)[0]["id"]
    r = run(env, "reinforce", rid, "correction",
            "--content", "deploy host is prod-01 in us-central",
            "--evidence", "ops confirmed")
    assert r.returncode == 0, r.stderr
    row = json.loads(run(env, "search", "deploy host", "--json").stdout)[0]
    assert row["content"] == "deploy host is prod-01 in us-central"


def test_reinforce_rejects_bad_signal(env):
    run(env, "capture", "--task", "t", "--outcome", "o")
    rid = json.loads(run(env, "search", "t", "--json").stdout)[0]["id"]
    r = run(env, "reinforce", rid, "bogus")
    assert r.returncode != 0


def test_analyze_writes_inferences(env):
    for i in range(3):
        run(env, "capture", "--task", f"deploy pipeline step {i}",
            "--outcome", "deploy pipeline ok")
    r = run(env, "analyze")
    assert r.returncode == 0, r.stderr
    assert "INFERENCE" not in r.stdout or "wrote" in r.stdout

    rows = json.loads(run(env, "search", "Recurring", "--json").stdout)
    assert rows, "analyze produced no INFERENCE rows"
    assert all(row["type"] == "INFERENCE" for row in rows)


def test_analyze_is_idempotent(env):
    for i in range(3):
        run(env, "capture", "--task", f"same words here {i}", "--outcome", "same words here")
    run(env, "analyze")
    first = len(json.loads(run(env, "search", "Recurring", "--json").stdout))
    run(env, "analyze")
    second = len(json.loads(run(env, "search", "Recurring", "--json").stdout))
    assert first == second, "analyze duplicated inference rows on rerun"


def test_promote_requires_verified_and_threshold(env):
    run(env, "capture", "--task", "low", "--outcome", "conf", "--confidence", "0.2")
    run(env, "capture", "--task", "high", "--outcome", "conf",
        "--confidence", "0.95", "--verified")
    r = run(env, "promote", "--threshold", "0.85")
    assert r.returncode == 0, r.stderr
    rules = json.loads(run(env, "search", "high", "--type", "RULE", "--json").stdout)
    assert rules, "verified high-confidence record was not promoted"
    lows = json.loads(run(env, "search", "low", "--type", "RULE", "--json").stdout)
    assert not lows, "unverified low-confidence record was promoted"


# --- defect: recap filtered records in Python after loading everything ----

def test_recap_by_session(env):
    run(env, "capture", "--task", "one", "--outcome", "x", "--session", "s-a")
    run(env, "capture", "--task", "two", "--outcome", "y", "--session", "s-b")
    r = run(env, "recap", "--session", "s-a")
    assert r.returncode == 0, r.stderr
    assert "one -> x" in r.stdout
    assert "two -> y" not in r.stdout


def test_recap_json_is_machine_readable(env):
    run(env, "capture", "--task", "one", "--outcome", "x")
    r = run(env, "recap", "--json")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert isinstance(data, list) and data[0]["content"] == "one -> x"


# --- defect: health checked a hardcoded path and crashed on missing DB ----

def test_health_ok(env):
    run(env, "capture", "--task", "t", "--outcome", "o")
    r = run(env, "health")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
    assert env["OMP_MEMORY_DB"] in r.stdout


def test_health_json(env):
    r = run(env, "health", "--json")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["status"] == "ok"
    assert data["db_path"] == env["OMP_MEMORY_DB"]


# --- defect: backup was a raw copy; guard called health() with a path ------

def test_backup_creates_restorable_snapshot(env):
    run(env, "capture", "--task", "t", "--outcome", "o")
    r = run(env, "backup")
    assert r.returncode == 0, r.stderr
    assert "backup:" in r.stdout
    backups = list(Path(env["OMP_MEMORY_BACKUP_DIR"]).glob("memory.db.bak.*"))
    assert len(backups) == 1


def test_backup_twice_keeps_both(env):
    run(env, "capture", "--task", "t", "--outcome", "o")
    assert run(env, "backup").returncode == 0
    assert run(env, "backup").returncode == 0
    backups = list(Path(env["OMP_MEMORY_BACKUP_DIR"]).glob("memory.db.bak.*"))
    assert len(backups) == 2, "second backup overwrote the first"


def test_repair_restores_after_corruption(env):
    run(env, "capture", "--task", "survive", "--outcome", "corruption")
    assert run(env, "backup").returncode == 0

    db_file = Path(env["OMP_MEMORY_DB"])
    for ext in ("", "-wal", "-shm"):
        candidate = Path(str(db_file) + ext)
        if candidate.exists():
            candidate.unlink()
    db_file.write_bytes(b"corrupted")

    r = run(env, "repair")
    assert r.returncode == 0, r.stderr

    rows = json.loads(run(env, "search", "survive", "--json").stdout)
    assert rows, "repair did not restore the record"


def test_repair_without_backup_fails_cleanly(env):
    Path(env["OMP_MEMORY_DB"]).parent.mkdir(parents=True, exist_ok=True)
    Path(env["OMP_MEMORY_DB"]).write_bytes(b"corrupted")
    r = run(env, "repair")
    assert r.returncode == 1
    assert "failed" in r.stdout


# --- defect: disk-check reported a missing path as zero and always failed -

def test_disk_check_creates_missing_dir(env):
    r = run(env, "disk", "1")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_disk_check_json(env):
    r = run(env, "disk", "1", "--json")
    data = json.loads(r.stdout)
    assert data["ok"] is True
    assert data["available_mb"] > 0


# --- defect: checkpoint stored nothing restorable -------------------------

def test_checkpoint_and_restore_via_cli(env):
    run(env, "capture", "--task", "checkpoint", "--outcome", "payload",
        "--session", "s-cp")
    r = run(env, "checkpoint", "--session", "s-cp")
    assert r.returncode == 0, r.stderr
    cp = json.loads(r.stdout)
    assert cp["record_count"] == 1

    r = run(env, "restore", cp["id"])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["restored_records"] == 1


# --- defect: context injection was not reachable from the shell layer -----

def test_context_emits_fenced_block(env):
    run(env, "capture", "--task", "fencing", "--outcome", "works")
    r = run(env, "context", "fencing")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("<memory-context>")
    assert r.stdout.rstrip().endswith("</memory-context>")


def test_context_no_match_prints_nothing(env):
    r = run(env, "context", "nothingmatchesthis")
    assert r.returncode == 0
    assert r.stdout == ""


# --- shims keep working ---------------------------------------------------

# The bin/ shims are bash scripts; they exist for POSIX installs only.
on_posix = os.name == "posix"


@pytest.mark.skipif(not on_posix, reason="bin/ shims are bash scripts")
@pytest.mark.parametrize("shim,args", [
    ("health-check.sh", []),
    ("disk-check.sh", ["1"]),
    ("secret-scan.sh", ["clean text"]),
])
def test_bin_shims_run(env, shim, args):
    r = subprocess.run([str(REPO_ROOT / "bin" / shim)] + args,
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not on_posix, reason="bin/ shims are bash scripts")
def test_analyze_shim_dispatches(env):
    r = subprocess.run([str(REPO_ROOT / "bin" / "analyze-session.sh"), "stats"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "total" in r.stdout


@pytest.mark.skipif(not on_posix, reason="bin/ shims are bash scripts")
def test_analyze_shim_rejects_unknown_mode(env):
    r = subprocess.run([str(REPO_ROOT / "bin" / "analyze-session.sh"), "bogus"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2


@pytest.mark.skipif(not on_posix, reason="bin/ shims are bash scripts")
def test_capture_shim_round_trip(env):
    r = subprocess.run(
        [str(REPO_ROOT / "bin" / "capture-session.sh"),
         "--task", "shim task", "--outcome", "shim outcome"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "captured" in r.stdout


def test_overview_parser_registered():
    from omp_memory.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["overview", "--port", "4999", "--no-open"])
    assert args.command == "overview"
    assert args.port == 4999
    assert args.no_open is True
