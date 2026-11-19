"""Idle watcher: capture session lessons after inactivity.

Polls the newest session log; when it has been quiet for IDLE_SECONDS
and not yet captured, runs capture_session(apply=True, reflect=True):
the capture writes the lessons and the reflect pass abstracts them
into patterns in the same connection. The state file records the last
captured mtime so a quiet session is only captured once.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .capture import latest_session, sessions_dir

IDLE_SECONDS = int(os.environ.get("OMP_MEMORY_IDLE_SECONDS", "600"))
POLL_SECONDS = int(os.environ.get("OMP_MEMORY_POLL_SECONDS", "60"))
SLEEP_INTERVAL = int(os.environ.get("OMP_MEMORY_SLEEP_INTERVAL", "3600"))

STATE_FILE = Path(
    os.environ.get(
        "OMP_MEMORY_WATCH_STATE",
        str(Path.home() / ".omp/memory/watch_state.json"),
    )
)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def idle_seconds(mtime: float, now: float) -> float:
    return now - mtime


def should_capture(mtime: float, now: float, captured_mtime: float | None) -> bool:
    """Capture when the log is idle and has new content since last capture."""
    if captured_mtime is not None and mtime <= captured_mtime:
        return False
    return idle_seconds(mtime, now) >= IDLE_SECONDS


def _session_project(session_path: Path) -> str:
    """Project id from the session folder name: -Documents-project-foo -> foo."""
    rel = session_path.relative_to(sessions_dir())
    parts = rel.parts[:-1]
    name = "-".join(parts).lstrip("-")
    if name.startswith("Documents-project-"):
        return name[len("Documents-project-"):]
    if name.startswith("project-"):
        return name[len("project-"):]
    return name


def capture_once(db_mod, conn, now: float | None = None) -> dict:
    """Capture the newest session if it qualifies. Returns a summary dict."""
    now = now if now is not None else time.time()
    session = latest_session()
    if session is None:
        return {"action": "none", "reason": "no-session"}
    mtime = session.stat().st_mtime
    state = load_state()
    captured = state.get("captured_mtime")
    if not should_capture(mtime, now, captured):
        return {"action": "none", "reason": "not-idle-or-already-captured"}

    from . import capture

    summary = capture.capture_session(db_mod, conn, session_path=session,
                                      apply=True, reflect=True)
    save_state({**state, "captured_mtime": mtime, "captured_at": now})
    return {
        "action": "captured",
        "session": str(session),
        "project": _session_project(session),
        **{k: v for k, v in summary.items() if k != "candidates"},
    }


def _notify(summary: dict) -> None:
    """Desktop notification so a capture is visible, not just silent.

    Best-effort: no DISPLAY or no notify-send means no notification,
    which must never break the capture itself. opt-in via
    OMP_MEMORY_NOTIFY=1.
    """
    if os.environ.get("OMP_MEMORY_NOTIFY") != "1":
        return
    refl = summary.get("reflect") or {}
    body = (f"{summary.get('written', 0)} lessons, "
            f"{refl.get('written', 0)} patterns")
    try:
        subprocess.run(
            ["notify-send", "-a", "honcho-pi", "Memory saved", body],
            timeout=10, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv=None) -> int:
    from . import db

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--once", "once"):
        conn = db.connect()
        try:
            print(json.dumps(capture_once(db, conn)))
        finally:
            conn.close()
        return 0

    running = {"flag": True}

    def stop(_sig, _frm):
        running["flag"] = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(f"[watch] polling {sessions_dir()} every {POLL_SECONDS}s, idle {IDLE_SECONDS}s", flush=True)
    conn = db.connect()
    last_sleep = time.time()
    try:
        while running["flag"]:
            try:
                result = capture_once(db, conn)
                if result["action"] != "none":
                    print(f"[watch] {json.dumps(result)}", flush=True)
                    _notify(result)
                if time.time() - last_sleep >= SLEEP_INTERVAL:
                    report = db.sleep_pass(conn, apply=True)
                    last_sleep = time.time()
                    print(f"[watch] sleep {json.dumps(report)}", flush=True)
            except Exception as exc:  # noqa: BLE001 - watcher must survive
                print(f"[watch] error: {exc}", file=sys.stderr, flush=True)
            time.sleep(POLL_SECONDS)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
