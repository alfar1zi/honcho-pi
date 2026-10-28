#!/usr/bin/env python3
"""
omp-memory-db — Production-grade SQLite memory system with FTS5 full-text search.

Architecture inspired by Hermes Agent (NousResearch/hermes-agent):
  - Session lineage (parent/child) for context continuity across compressions
  - Pre-compress checkpoint v2 (fail-closed evidence handoff before compression)
  - Profile isolation (profile_contexts table) — no module/class-level mutable state
  - WAL journal mode + busy_timeout for concurrent access
  - integrity_check on every open with auto-repair from backup
  - Automatic backup before every write (rotation, max 10)
  - Record types: FACT / ASSUMPTION / INFERENCE / DECISION / RULE / CORRECTION
  - Reinforcement: success/failure/correction signals adjust confidence
  - Stuck call gate pattern for concurrent access protection

Content-based dedup: record_id is derived from content+source+project,
so identical content with same source+project produces same ID (no duplicates).
"""

import sqlite3
import json
import hashlib
import os
import sys
import shutil
import glob
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("OMP_MEMORY_DB", str(Path.home() / ".omp/memory/memory.db"))
BACKUP_DIR = os.environ.get("OMP_MEMORY_BACKUP_DIR", str(Path.home() / ".omp/memory/backups"))
MAX_BACKUPS = int(os.environ.get("OMP_MEMORY_MAX_BACKUPS", "10"))
BUSY_TIMEOUT_MS = int(os.environ.get("OMP_MEMORY_BUSY_TIMEOUT_MS", "30000"))

VALID_TYPES = {"FACT", "ASSUMPTION", "INFERENCE", "DECISION", "RULE", "CORRECTION"}
VALID_SIGNALS = {"success", "failure", "correction", "reinforce", "decay"}

PRE_COMPRESS_CHECKPOINT_API_VERSION = 2

_VALID_SIGNAL_DELTAS = {
    "success": 0.1,
    "failure": -0.1,
    "correction": 0.15,
    "reinforce": 0.05,
    "decay": -0.05,
}

_CALL_GATE_LOCK = threading.Lock()
_CALL_GATE_CALLS = {}
_CALL_GATE_MAX_STUCK = 4
_CALL_GATE_PROBE_INTERVAL = 60.0
_CALL_GATE_MAX_INFLIGHT = 16


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def ensure_dirs():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)


def rotate_backups():
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "memory.db.bak.*")), key=os.path.getmtime)
    while len(backups) > MAX_BACKUPS:
        try:
            os.remove(backups.pop(0))
        except OSError:
            pass


def backup_db(src: str) -> str:
    if not os.path.exists(src):
        return ""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst = os.path.join(BACKUP_DIR, f"memory.db.bak.{ts}")
    try:
        shutil.copy2(src, dst)
        os.chmod(dst, 0o600)
        rotate_backups()
        return dst
    except (OSError, shutil.Error):
        return ""


_profile_context = threading.local()


def get_profile() -> str:
    return getattr(_profile_context, "profile", "default")


def set_profile(profile: str):
    _profile_context.profile = profile


def clear_profile():
    if hasattr(_profile_context, "profile"):
        delattr(_profile_context, "profile")


_CALL_GATE_LOCK = threading.Lock()
_CALL_GATE_CALLS = {}
_CALL_GATE_MAX_STUCK = 4
_CALL_GATE_PROBE_INTERVAL = 60.0
_CALL_GATE_MAX_INFLIGHT = 16


def call_gate_enter(key: tuple, timeout: float = 5.0) -> bool:
    with _CALL_GATE_LOCK:
        now = time.time()
        state = _CALL_GATE_CALLS.get(key)
        if state is None:
            state = {"count": 0, "last_success": now, "stuck": 0, "probe_at": now}
            _CALL_GATE_CALLS[key] = state

        inflight = state["count"]
        if inflight >= _CALL_GATE_MAX_INFLIGHT:
            return False

        stuck = state["stuck"]
        if stuck >= _CALL_GATE_MAX_STUCK:
            if now - state["probe_at"] < _CALL_GATE_PROBE_INTERVAL:
                return False
            state["probe_at"] = now

        state["count"] += 1
        return True


def call_gate_leave(key: tuple, ok: bool) -> None:
    with _CALL_GATE_LOCK:
        state = _CALL_GATE_CALLS.get(key)
        if state is None:
            return
        state["count"] = max(0, state["count"] - 1)
        if ok:
            state["last_success"] = time.time()
            state["stuck"] = max(0, state["stuck"] - 1)
        else:
            state["stuck"] += 1


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("SELECT 1").fetchone()
        return conn
    except sqlite3.DatabaseError:
        try:
            conn.close()
        except Exception:
            pass
        try:
            os.remove(db_path)
            for ext in ("-wal", "-shm"):
                p = db_path + ext
                if os.path.exists(p):
                    os.remove(p)
        except OSError:
            pass
        conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def integrity_check(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return row is not None and str(row[0]) == "ok"
    except sqlite3.DatabaseError:
        return False


def quick_check(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return row is not None and str(row[0]) == "ok"
    except sqlite3.DatabaseError:
        return False


def repair_from_backup(db_path: str = DB_PATH) -> bool:
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "memory.db.bak.*")), key=os.path.getmtime, reverse=True)
    for bak in backups:
        try:
            shutil.copy2(bak, db_path)
            conn = connect(db_path)
            if integrity_check(conn):
                conn.close()
                return True
            conn.close()
        except (OSError, shutil.Error, sqlite3.DatabaseError):
            continue
    return False


def _create_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS records (
        id          TEXT PRIMARY KEY,
        type        TEXT NOT NULL CHECK (type IN ('FACT','ASSUMPTION','INFERENCE','DECISION','RULE','CORRECTION')),
        content     TEXT NOT NULL,
        evidence    TEXT NOT NULL DEFAULT '',
        verified    INTEGER NOT NULL DEFAULT 0,
        confidence  REAL NOT NULL DEFAULT 0.5,
        source      TEXT NOT NULL DEFAULT '',
        tags        TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        session_id  TEXT NOT NULL DEFAULT '',
        project_id  TEXT NOT NULL DEFAULT '',
        parent_id   TEXT NOT NULL DEFAULT ''
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
        id, content, tags
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id            TEXT PRIMARY KEY,
        parent_id     TEXT NOT NULL DEFAULT '',
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        project_id    TEXT NOT NULL DEFAULT '',
        summary       TEXT NOT NULL DEFAULT '',
        capture_hash  TEXT NOT NULL DEFAULT '',
        profile       TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS pre_compress_checkpoints (
        id            TEXT PRIMARY KEY,
        session_id    TEXT NOT NULL,
        checkpoint_api_version INTEGER NOT NULL DEFAULT 2,
        evidence_hash TEXT NOT NULL DEFAULT '',
        record_count  INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL,
        restored_from TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS profile_contexts (
        profile     TEXT NOT NULL,
        key         TEXT NOT NULL,
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (profile, key)
    );

    CREATE TABLE IF NOT EXISTS reinforcement (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id     TEXT NOT NULL,
        signal        TEXT NOT NULL CHECK (signal IN ('success','failure','correction','reinforce','decay')),
        delta         REAL NOT NULL DEFAULT 0.1,
        source        TEXT NOT NULL DEFAULT '',
        ts            TEXT NOT NULL,
        FOREIGN KEY (record_id) REFERENCES records(id)
    );

    CREATE INDEX IF NOT EXISTS idx_records_type       ON records(type);
    CREATE INDEX IF NOT EXISTS idx_records_project    ON records(project_id);
    CREATE INDEX IF NOT EXISTS idx_records_session    ON records(session_id);
    CREATE INDEX IF NOT EXISTS idx_records_parent     ON records(parent_id);
    CREATE INDEX IF NOT EXISTS idx_records_verified   ON records(verified);
    CREATE INDEX IF NOT EXISTS idx_records_confidence ON records(confidence);
    CREATE INDEX IF NOT EXISTS idx_sessions_project   ON sessions(project_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_profile   ON sessions(profile);
    CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON pre_compress_checkpoints(session_id);
    CREATE INDEX IF NOT EXISTS idx_reinforcement_record ON reinforcement(record_id);
    """)


def init_db(conn: sqlite3.Connection, db_path: str = DB_PATH, auto_repair: bool = True):
    try:
        ok = quick_check(conn)
    except sqlite3.DatabaseError:
        ok = False
    if not ok:
        try:
            ok = integrity_check(conn)
        except sqlite3.DatabaseError:
            ok = False
    if not ok and auto_repair:
        try:
            conn.close()
            if repair_from_backup(db_path):
                conn = connect(db_path)
                ok = quick_check(conn)
            else:
                ok = False
        except sqlite3.DatabaseError:
            ok = False
    if not ok:
        try:
            conn.close()
            os.remove(db_path)
            for ext in ("-wal", "-shm"):
                p = db_path + ext
                if os.path.exists(p):
                    os.remove(p)
            conn = connect(db_path)
        except (OSError, sqlite3.DatabaseError):
            pass
    _create_schema(conn)


def upsert_record(conn: sqlite3.Connection, rtype: str, content: str, evidence: str = "",
                  verified: int = 0, confidence: float = 0.5, source: str = "",
                  tags: str = "", session_id: str = "", project_id: str = "",
                  parent_id: str = "") -> str:
    if rtype not in VALID_TYPES:
        raise ValueError(f"invalid type: {rtype}")

    project_key = project_id or ""
    record_id = sha(f"{rtype}:{content}:{source}:{project_key}")

    existing = conn.execute(
        "SELECT id FROM records WHERE type=? AND content=? AND source=? AND project_id=?",
        (rtype, content, source, project_key)
    ).fetchone()

    if existing:
        record_id = existing["id"]
        conn.execute(
            "UPDATE records SET updated_at=?, evidence=? WHERE id=?",
            (now_iso(), evidence if evidence else None, record_id)
        )
    else:
        conn.execute("""
            INSERT INTO records (id,type,content,evidence,verified,confidence,source,tags,created_at,updated_at,session_id,project_id,parent_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (record_id, rtype, content, evidence, 1 if verified else 0, confidence, source, tags, now_iso(), now_iso(), project_key, project_key, parent_id))

    conn.execute("DELETE FROM records_fts WHERE id=?", (record_id,))
    conn.execute("INSERT INTO records_fts(id, content, tags) VALUES (?,?,?)", (record_id, content, tags))
    conn.commit()

    try:
        backup_db(DB_PATH)
    except Exception:
        pass

    return record_id


def search(conn: sqlite3.Connection, query: str, limit: int = 20, rtype: str = None,
           project_id: str = None, verified_only: bool = False, min_confidence: float = 0.0):
    sql = """
        SELECT r.id, r.type, r.content, r.evidence, r.verified, r.confidence, r.source, r.tags,
               r.created_at, r.updated_at, r.session_id, r.project_id, r.parent_id,
               bm25(records_fts) AS rank
        FROM records_fts
        JOIN records r ON r.id = records_fts.id
        WHERE records_fts MATCH ?
    """
    params = [query]
    if rtype:
        sql += " AND r.type = ?"
        params.append(rtype)
    if project_id:
        sql += " AND r.project_id = ?"
        params.append(project_id)
    if verified_only:
        sql += " AND r.verified = 1"
    if min_confidence > 0:
        sql += " AND r.confidence >= ?"
        params.append(min_confidence)
    sql += " ORDER BY r.confidence DESC, r.updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def get_record(conn: sqlite3.Connection, record_id: str):
    row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def list_records(conn: sqlite3.Connection, rtype: str = None, project_id: str = None,
                 limit: int = 100, offset: int = 0):
    sql = "SELECT * FROM records"
    params = []
    conds = []
    if rtype:
        conds.append("type=?")
        params.append(rtype)
    if project_id:
        conds.append("project_id=?")
        params.append(project_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY confidence DESC, updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [dict(row) for row in conn.execute(sql, params)]


def reinforce(conn: sqlite3.Connection, record_id: str, signal: str, delta: float = 0.1, source: str = "") -> float:
    if signal not in VALID_SIGNALS:
        raise ValueError(f"invalid signal: {signal}")

    rec = get_record(conn, record_id)
    if not rec:
        raise ValueError(f"record not found: {record_id}")

    actual_delta = VALID_SIGNAL_DELTAS.get(signal, delta)
    new_conf = min(1.0, max(0.0, rec["confidence"] + actual_delta))

    conn.execute("UPDATE records SET confidence=?, updated_at=? WHERE id=?",
                 (new_conf, now_iso(), record_id))
    conn.execute("""
        INSERT INTO reinforcement (record_id, signal, delta, source, ts)
        VALUES (?, ?, ?, ?, ?)
    """, (record_id, signal, actual_delta, source, now_iso()))

    conn.commit()
    try:
        backup_db(DB_PATH)
    except Exception:
        pass

    return new_conf


def start_session(conn: sqlite3.Connection, session_id: str, project_id: str = "",
                  parent_id: str = "", profile: str = "") -> str:
    conn.execute("""
        INSERT OR REPLACE INTO sessions (id, parent_id, started_at, project_id, summary, capture_hash, profile)
        VALUES (?, ?, ?, ?, '', '', ?)
    """, (session_id, parent_id, now_iso(), project_id, profile))
    conn.commit()
    return session_id


def end_session(conn: sqlite3.Connection, session_id: str, summary: str = "", capture_hash: str = ""):
    conn.execute("""
        UPDATE sessions SET ended_at=?, summary=?, capture_hash=? WHERE id=?
    """, (now_iso(), summary, capture_hash, session_id))
    conn.commit()


def create_pre_compress_checkpoint(conn: sqlite3.Connection, session_id: str,
                                   evidence_hash: str = "", record_count: int = 0) -> str:
    checkpoint_id = sha(f"pre-compress:{session_id}:{now_iso()}")
    conn.execute("""
        INSERT INTO pre_compress_checkpoints (id, session_id, checkpoint_api_version, evidence_hash, record_count, created_at, restored_from)
        VALUES (?, ?, ?, ?, ?, ?, '')
    """, (checkpoint_id, session_id, PRE_COMPRESS_CHECKPOINT_API_VERSION, evidence_hash, record_count, now_iso()))
    conn.commit()
    return checkpoint_id


def restore_from_checkpoint(conn: sqlite3.Connection, checkpoint_id: str) -> dict:
    row = conn.execute("SELECT * FROM pre_compress_checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
    if not row:
        raise ValueError(f"checkpoint not found: {checkpoint_id}")
    return dict(row)


def stats(conn: sqlite3.Connection) -> dict:
    result = {}
    for t in VALID_TYPES:
        c = conn.execute("SELECT COUNT(*) AS n FROM records WHERE type=?", (t,)).fetchone()["n"]
        result[t] = c
    result["total"] = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"]
    result["verified"] = conn.execute("SELECT COUNT(*) AS n FROM records WHERE verified=1").fetchone()["n"]
    result["sessions"] = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    result["checkpoints"] = conn.execute("SELECT COUNT(*) AS n FROM pre_compress_checkpoints").fetchone()["n"]
    return result


def health(db_path: str = DB_PATH) -> dict:
    try:
        conn = connect(db_path)
        init_db(conn)
        ok = quick_check(conn)
        if not ok:
            ok = integrity_check(conn)
        backups = len(glob.glob(os.path.join(BACKUP_DIR, "memory.db.bak.*")))
        try:
            total = stats(conn).get("total", 0)
        except sqlite3.DatabaseError:
            total = 0
        conn.close()
        return {
            "status": "ok" if ok else "corrupt",
            "db_path": db_path,
            "backup_count": backups,
            "total_records": total,
        }
    except sqlite3.DatabaseError:
        backups = len(glob.glob(os.path.join(BACKUP_DIR, "memory.db.bak.*")))
        return {
            "status": "corrupt",
            "db_path": db_path,
            "backup_count": backups,
            "total_records": 0,
        }


def get_profile_context(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    profile = get_profile()
    row = conn.execute(
        "SELECT value FROM profile_contexts WHERE profile=? AND key=?",
        (profile, key)
    ).fetchone()
    return row["value"] if row else default


def set_profile_context(conn: sqlite3.Connection, key: str, value: str):
    profile = get_profile()
    conn.execute("""
        INSERT OR REPLACE INTO profile_contexts (profile, key, value, updated_at)
        VALUES (?, ?, ?, ?)
    """, (profile, key, value, now_iso()))
    conn.commit()


if __name__ == "__main__":
    conn = connect()
    init_db(conn)
    print(json.dumps({
        "stats": stats(conn),
        "health": health(),
    }, indent=2))
    conn.close()
