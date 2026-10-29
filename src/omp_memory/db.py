#!/usr/bin/env python3
"""
omp-memory-db - SQLite-backed memory system with FTS5 full-text search.

Design constraints (verified by tests/test_memory_db.py):
- Opening a DB never deletes it. Corruption is reported, not silently repaired
  by unlinking the file.
- Backups are consistent snapshots taken with VACUUM INTO, never a raw copy of
  a live WAL database.
- FTS5 is maintained by triggers on the records table, so any write path keeps
  the index in sync. Search input is tokenised and quoted before MATCH.
- Profile isolation lives in the profile_contexts table; no module-level
  mutable state besides per-thread profile selection.
- Record ids are content-addressed: sha256 of type:content:source:project.
- Confidence is clamped to [0, 1]; reinforcement signals are validated.

Record types: FACT / ASSUMPTION / INFERENCE / DECISION / RULE / CORRECTION.
"""
import sqlite3
import json
import hashlib
import os
import glob
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("OMP_MEMORY_DB", str(Path.home() / ".omp/memory/memory.db"))
BACKUP_DIR = os.environ.get("OMP_MEMORY_BACKUP_DIR", str(Path.home() / ".omp/memory/backups"))
MAX_BACKUPS = int(os.environ.get("OMP_MEMORY_MAX_BACKUPS", "10"))
BUSY_TIMEOUT_MS = int(os.environ.get("OMP_MEMORY_BUSY_TIMEOUT_MS", "30000"))

VALID_TYPES = {"FACT", "ASSUMPTION", "INFERENCE", "DECISION", "RULE", "CORRECTION"}
VALID_SIGNALS = {"success", "failure", "correction", "reinforce", "decay"}
SIGNAL_DELTAS = {
    "success": 0.1,
    "failure": -0.1,
    "correction": 0.15,
    "reinforce": 0.05,
    "decay": -0.05,
}
PRE_COMPRESS_CHECKPOINT_API_VERSION = 2
SCHEMA_VERSION = 2

_profile_context = threading.local()


# --- helpers -----------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def ensure_dirs():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)


def get_profile() -> str:
    return getattr(_profile_context, "profile", "default")


def set_profile(profile: str):
    _profile_context.profile = profile


def clear_profile():
    if hasattr(_profile_context, "profile"):
        delattr(_profile_context, "profile")


def db_path_of(conn: sqlite3.Connection) -> str:
    """Resolve the main database file of a live connection."""
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return DB_PATH
    try:
        return row[2] or DB_PATH
    except (IndexError, TypeError):
        return DB_PATH


def record_id_for(rtype: str, content: str, source: str, project_id: str) -> str:
    """Content-addressed id; identical inputs always yield the same id."""
    return sha(f"{rtype}:{content}:{source or ''}:{project_id or ''}")


# --- backups -----------------------------------------------------------------

def rotate_backups():
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "memory.db.bak.*")), key=os.path.getmtime)
    while len(backups) > MAX_BACKUPS:
        try:
            os.remove(backups.pop(0))
        except OSError:
            pass


def backup_db(src: str) -> str:
    """Consistent snapshot of src via VACUUM INTO. Returns the path, or "".

    A raw file copy of a WAL database can capture a torn state: the main file
    may not yet contain recently committed pages. VACUUM INTO writes a
    compacted, self-consistent image.
    """
    if not os.path.exists(src):
        return ""
    ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for attempt in range(5):
        dst = os.path.join(BACKUP_DIR, f"memory.db.bak.{stamp}" + (f".{attempt}" if attempt else ""))
        if os.path.exists(dst):
            continue
        try:
            conn = sqlite3.connect(src, timeout=BUSY_TIMEOUT_MS / 1000.0)
            try:
                conn.execute("VACUUM INTO ?", (dst,))
            finally:
                conn.close()
            os.chmod(dst, 0o600)
            rotate_backups()
            return dst
        except sqlite3.Error:
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            continue
    return ""


def repair_from_backup(db_path: str = DB_PATH) -> bool:
    """Restore db_path from the newest usable backup. Never deletes db_path.

    VACUUM INTO reads a source database and writes a compacted image, so the
    backup is the source and a temporary file is the destination. The target is
    then replaced atomically, so a failure mid-repair cannot leave the database
    missing.
    """
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "memory.db.bak.*")),
                     key=os.path.getmtime, reverse=True)
    for bak in backups:
        if not os.path.exists(bak):
            continue
        tmp = f"{db_path}.repair-{os.getpid()}"
        try:
            src = sqlite3.connect(bak, timeout=BUSY_TIMEOUT_MS / 1000.0)
            try:
                row = src.execute("PRAGMA quick_check").fetchone()
                usable = row is not None and str(row[0]) == "ok"
                if usable:
                    src.execute("VACUUM INTO ?", (tmp,))
            finally:
                src.close()
            if not usable:
                continue
            os.chmod(tmp, 0o600)
            os.replace(tmp, db_path)
            for ext in ("-wal", "-shm"):
                stale = db_path + ext
                if os.path.exists(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
            return True
        except (sqlite3.Error, OSError):
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            continue
    return False


# --- connections -------------------------------------------------------------

def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open a connection. Raises on failure; the file is never removed."""
    ensure_dirs()
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


# --- schema ------------------------------------------------------------------

def _create_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL
    );
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
        id, content, evidence, tags
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
        project_id    TEXT NOT NULL DEFAULT '',
        checkpoint_api_version INTEGER NOT NULL DEFAULT 2,
        evidence_hash TEXT NOT NULL DEFAULT '',
        record_count  INTEGER NOT NULL DEFAULT 0,
        payload       TEXT NOT NULL DEFAULT '',
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


def _create_fts_triggers(conn: sqlite3.Connection):
    conn.executescript("""
    DROP TRIGGER IF EXISTS records_ai;
    DROP TRIGGER IF EXISTS records_ad;
    DROP TRIGGER IF EXISTS records_au;
    CREATE TRIGGER records_ai AFTER INSERT ON records BEGIN
        INSERT INTO records_fts(id, content, evidence, tags)
        VALUES (new.id, new.content, new.evidence, new.tags);
    END;
    CREATE TRIGGER records_ad AFTER DELETE ON records BEGIN
        DELETE FROM records_fts WHERE id = old.id;
    END;
    CREATE TRIGGER records_au AFTER UPDATE ON records BEGIN
        DELETE FROM records_fts WHERE id = old.id;
        INSERT INTO records_fts(id, content, evidence, tags)
        VALUES (new.id, new.content, new.evidence, new.tags);
    END;
    """)


def _current_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _migrate(conn: sqlite3.Connection):
    """Bring an older database up to SCHEMA_VERSION. Idempotent."""
    version = _current_schema_version(conn)
    if version >= SCHEMA_VERSION:
        return

    if version < 1:
        # v1: FTS without the evidence column. Rebuild the index from records.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records_fts)").fetchall()]
        if cols and "evidence" not in cols:
            conn.execute("DROP TABLE records_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE records_fts USING fts5(id, content, evidence, tags)"
            )
            conn.execute(
                "INSERT INTO records_fts(id, content, evidence, tags)"
                " SELECT id, content, evidence, tags FROM records"
            )
        _create_fts_triggers(conn)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (1)")
        conn.commit()

    if version < 2:
        # v2: checkpoints carry a restorable payload and a project id.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(pre_compress_checkpoints)").fetchall()]
        if cols and "payload" not in cols:
            conn.execute("ALTER TABLE pre_compress_checkpoints ADD COLUMN payload TEXT NOT NULL DEFAULT ''")
        if cols and "project_id" not in cols:
            conn.execute("ALTER TABLE pre_compress_checkpoints ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (2)")
        conn.commit()


def init_db(conn: sqlite3.Connection, db_path: str = None, auto_repair: bool = True):
    """Create or migrate the schema on an open connection.

    Raises sqlite3.DatabaseError when the database is unusable. The caller's
    connection is never closed and the file is never removed; a corrupt
    database is the operator's decision, made explicitly via repair_from_backup.
    """
    if not quick_check(conn):
        if not integrity_check(conn):
            if auto_repair and db_path and repair_from_backup(db_path):
                # repair worked on the file; the caller must reconnect
                raise sqlite3.DatabaseError(
                    f"database was corrupt and has been repaired from backup: {db_path}"
                )
            raise sqlite3.DatabaseError(f"database is corrupt: {db_path_of(conn)}")
    _create_schema(conn)
    _migrate(conn)
    conn.commit()


# --- records -----------------------------------------------------------------

def upsert_record(conn: sqlite3.Connection, rtype: str, content: str, evidence: str = "",
                  verified: int = 0, confidence: float = 0.5, source: str = "",
                  tags: str = "", session_id: str = "", project_id: str = "",
                  parent_id: str = "") -> str:
    if rtype not in VALID_TYPES:
        raise ValueError(f"invalid type: {rtype}")
    if not content:
        raise ValueError("content must not be empty")

    confidence = min(1.0, max(0.0, float(confidence)))
    project_key = project_id or ""
    session_key = session_id or ""
    record_id = record_id_for(rtype, content, source, project_key)

    existing = conn.execute(
        "SELECT id FROM records WHERE type=? AND content=? AND source=? AND project_id=?",
        (rtype, content, source, project_key)
    ).fetchone()

    if existing:
        record_id = existing["id"]
        conn.execute(
            "UPDATE records SET updated_at=?, evidence=?, verified=?, confidence=?,"
            " tags=?, session_id=?, parent_id=? WHERE id=?",
            (now_iso(), evidence or "", 1 if verified else 0, confidence,
             tags or "", session_key, parent_id or "", record_id)
        )
    else:
        conn.execute("""
            INSERT INTO records
                (id,type,content,evidence,verified,confidence,source,tags,
                 created_at,updated_at,session_id,project_id,parent_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (record_id, rtype, content, evidence or "", 1 if verified else 0, confidence,
              source or "", tags or "", now_iso(), now_iso(), session_key, project_key, parent_id or ""))

    conn.commit()
    return record_id


def _fts_query(query: str) -> str:
    """Neutralise FTS5 operator characters by quoting each whitespace token.

    A bare MATCH string is a query language: quotes, *, -, NEAR( and boolean
    keywords either raise or silently change meaning. Quoting every token keeps
    the original AND-of-terms behaviour for ordinary input and makes operator
    input inert.
    """
    tokens = query.split()
    if not tokens:
        return '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def search(conn: sqlite3.Connection, query: str, limit: int = 20, rtype: str = None,
           project_id: str = None, verified_only: bool = False, min_confidence: float = 0.0):
    if not query or not query.strip():
        return []
    sql = """
        SELECT r.id, r.type, r.content, r.evidence, r.verified, r.confidence, r.source, r.tags,
               r.created_at, r.updated_at, r.session_id, r.project_id, r.parent_id,
               bm25(records_fts) AS rank
        FROM records_fts
        JOIN records r ON r.id = records_fts.id
        WHERE records_fts MATCH ?
    """
    params = [_fts_query(query)]
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
    sql += " ORDER BY rank, r.confidence DESC, r.updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def get_record(conn: sqlite3.Connection, record_id: str):
    row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def find_record(conn: sqlite3.Connection, rtype: str, content: str,
                source: str = "", project_id: str = ""):
    """Look up a record by its content-addressed identity."""
    return get_record(conn, record_id_for(rtype, content, source, project_id))


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


def reinforce(conn: sqlite3.Connection, record_id: str, signal: str,
              delta: float = None, source: str = "") -> float:
    """Apply a reinforcement signal. Explicit delta overrides the canonical one."""
    if signal not in VALID_SIGNALS:
        raise ValueError(f"invalid signal: {signal}")

    rec = get_record(conn, record_id)
    if not rec:
        raise ValueError(f"record not found: {record_id}")

    actual_delta = float(delta) if delta is not None else SIGNAL_DELTAS[signal]
    new_conf = min(1.0, max(0.0, rec["confidence"] + actual_delta))

    conn.execute("UPDATE records SET confidence=?, updated_at=? WHERE id=?",
                 (new_conf, now_iso(), record_id))
    conn.execute("""
        INSERT INTO reinforcement (record_id, signal, delta, source, ts)
        VALUES (?, ?, ?, ?, ?)
    """, (record_id, signal, actual_delta, source, now_iso()))

    conn.commit()
    return new_conf


# --- sessions ----------------------------------------------------------------

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


def list_sessions(conn: sqlite3.Connection, project_id: str = None, limit: int = 100):
    sql = "SELECT * FROM sessions"
    params = []
    if project_id:
        sql += " WHERE project_id=?"
        params.append(project_id)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


# --- pre-compress checkpoints ------------------------------------------------

def create_pre_compress_checkpoint(conn: sqlite3.Connection, session_id: str,
                                   project_id: str = "", evidence_hash: str = "") -> dict:
    """Snapshot the evidence a compressed session needs to rebuild context.

    The payload is the full record set for the session and its project, stored
    as JSON so restore does not depend on the live database.
    """
    rows = conn.execute(
        "SELECT * FROM records WHERE session_id=? OR project_id=? ORDER BY created_at",
        (session_id, project_id or "\x00none")
    ).fetchall()
    records = [dict(r) for r in rows]

    checkpoint_id = sha(f"pre-compress:{session_id}:{now_iso()}")
    conn.execute("""
        INSERT INTO pre_compress_checkpoints
            (id, session_id, project_id, checkpoint_api_version, evidence_hash,
             record_count, payload, created_at, restored_from)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
    """, (checkpoint_id, session_id, project_id or "", PRE_COMPRESS_CHECKPOINT_API_VERSION,
          evidence_hash, len(records), json.dumps(records, ensure_ascii=False), now_iso()))
    conn.commit()

    return {
        "id": checkpoint_id,
        "session_id": session_id,
        "project_id": project_id or "",
        "api_version": PRE_COMPRESS_CHECKPOINT_API_VERSION,
        "record_count": len(records),
        "evidence_hash": evidence_hash,
    }


def restore_from_checkpoint(conn: sqlite3.Connection, checkpoint_id: str) -> dict:
    """Write a checkpoint's records back into the database. Idempotent."""
    row = conn.execute("SELECT * FROM pre_compress_checkpoints WHERE id=?",
                       (checkpoint_id,)).fetchone()
    if not row:
        raise ValueError(f"checkpoint not found: {checkpoint_id}")
    row = dict(row)

    records = json.loads(row["payload"]) if row["payload"] else []
    restored = 0
    for rec in records:
        conn.execute("""
            INSERT OR REPLACE INTO records
                (id,type,content,evidence,verified,confidence,source,tags,
                 created_at,updated_at,session_id,project_id,parent_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (rec["id"], rec["type"], rec["content"], rec.get("evidence", ""),
              rec.get("verified", 0), rec.get("confidence", 0.5), rec.get("source", ""),
              rec.get("tags", ""), rec.get("created_at", now_iso()), rec.get("updated_at", now_iso()),
              rec.get("session_id", ""), rec.get("project_id", ""), rec.get("parent_id", "")))
        restored += 1

    conn.execute("UPDATE pre_compress_checkpoints SET restored_from=? WHERE id=?",
                 (now_iso(), checkpoint_id))
    conn.commit()

    return {
        "id": checkpoint_id,
        "session_id": row["session_id"],
        "restored_records": restored,
        "api_version": row["checkpoint_api_version"],
    }


# --- stats / health ----------------------------------------------------------

def stats(conn: sqlite3.Connection) -> dict:
    result = {}
    for t in sorted(VALID_TYPES):
        c = conn.execute("SELECT COUNT(*) AS n FROM records WHERE type=?", (t,)).fetchone()["n"]
        result[t] = c
    result["total"] = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"]
    result["verified"] = conn.execute("SELECT COUNT(*) AS n FROM records WHERE verified=1").fetchone()["n"]
    result["sessions"] = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    result["checkpoints"] = conn.execute("SELECT COUNT(*) AS n FROM pre_compress_checkpoints").fetchone()["n"]
    result["reinforcements"] = conn.execute("SELECT COUNT(*) AS n FROM reinforcement").fetchone()["n"]
    return result


def health(conn: sqlite3.Connection) -> dict:
    ok = quick_check(conn) or integrity_check(conn)
    backups = len(glob.glob(os.path.join(BACKUP_DIR, "memory.db.bak.*")))
    try:
        total = stats(conn).get("total", 0)
    except sqlite3.DatabaseError:
        total = 0
    return {
        "status": "ok" if ok else "corrupt",
        "db_path": db_path_of(conn),
        "backup_count": backups,
        "total_records": total,
    }


# --- profile-scoped context --------------------------------------------------

def get_profile_context(conn: sqlite3.Connection, key: str, default=None):
    """JSON-encoded per-profile value. Returns `default` when absent."""
    profile = get_profile()
    row = conn.execute(
        "SELECT value FROM profile_contexts WHERE profile=? AND key=?",
        (profile, key)
    ).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_profile_context(conn: sqlite3.Connection, key: str, value):
    profile = get_profile()
    conn.execute("""
        INSERT OR REPLACE INTO profile_contexts (profile, key, value, updated_at)
        VALUES (?, ?, ?, ?)
    """, (profile, key, json.dumps(value, ensure_ascii=False), now_iso()))
    conn.commit()


if __name__ == "__main__":
    conn = connect()
    init_db(conn)
    print(json.dumps({
        "stats": stats(conn),
        "health": health(conn),
    }, indent=2))
    conn.close()
