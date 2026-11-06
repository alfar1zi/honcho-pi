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
import difflib
import sqlite3
import subprocess
import re
import unicodedata
import json
import hashlib
import os
import glob
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = os.environ.get("OMP_MEMORY_DB", str(Path.home() / ".omp/memory/memory.db"))
BACKUP_DIR = os.environ.get("OMP_MEMORY_BACKUP_DIR", str(Path.home() / ".omp/memory/backups"))
MAX_BACKUPS = int(os.environ.get("OMP_MEMORY_MAX_BACKUPS", "10"))
BUSY_TIMEOUT_MS = int(os.environ.get("OMP_MEMORY_BUSY_TIMEOUT_MS", "30000"))


def _restrict_to_owner(path: str) -> None:
    """Best-effort 0600. POSIX honours it; Windows has no such mode bits and chmod only touches
    the read-only flag, so a failure or a no-op there must not abort a backup or a repair."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


VALID_TYPES = {"FACT", "ASSUMPTION", "INFERENCE", "DECISION", "RULE", "CORRECTION"}
VALID_SIGNALS = {"success", "failure", "correction", "reinforce", "decay"}
SIGNAL_DELTAS = {
    "success": 0.1,
    "failure": -0.1,
    "correction": 0.15,
    "reinforce": 0.05,
    "decay": -0.05,
}

# Heat weights are fixed, not fitted. Fitting them to one corpus would bake that
# corpus's quirks into the ranking and make the numbers impossible to explain
# per record; fixed weights keep heat decomposable and comparable across
# sessions. They sum to 1.0 so heat stays inside [0, 1].
HEAT_WEIGHTS = {"confidence": 0.45, "recency": 0.25, "reinforcement": 0.30}
HEAT_HALF_LIFE_DAYS = 30.0
PROMOTE_HEAT = 0.80
PROMOTABLE_TYPES = ("INFERENCE", "ASSUMPTION")
DECAY_FLOOR = 0.15
# Power-law forgetting: retention = (1 + d/tau)**-beta, normalized so the
# 30-day point matches the old exponential half-life. Empirical forgetting
# curves follow a power law (Wixted & Ebbesen 1991; Murre & Dros 2015):
# fast early drop, long tail for genuinely important old records.
DECAY_POWER_TAU_DAYS = 10.0
DECAY_POWER_BETA = 0.5
# Schema-consistent prior (Tse et al. 2007): a new record that agrees with
# what high-confidence memory already holds starts closer to that memory's
# confidence; a contradictory one starts lower and must earn verification.
SCHEMA_PRIOR_WEIGHT = 0.2
FORGET_FLOOR_SLACK = 0.01
FORGET_MIN_AGE_DAYS = 180
FORGET_WINDOW_DAYS = 365
PRE_COMPRESS_CHECKPOINT_API_VERSION = 2
SCHEMA_VERSION = 7

# Recall reranking weights. Lexical is dominant (0.70) because recall is
# primarily an inquiry of relevance. Heat is moderate (0.25) so stale yet
# relevant records do not sink while breaking ties among equally relevant
# facts; confidence is already inside heat, so it gets no second term.
# Type prior is small (0.05) so trusted kinds (RULE, FACT) outrank INFERENCE
# without overriding topical relevance.
RECALL_WEIGHTS = {"lexical": 0.70, "heat": 0.25, "type_prior": 0.05}
TYPE_PRIOR = {
    "RULE": 1.0,
    "FACT": 1.0,
    "CORRECTION": 0.85,
    "DECISION": 0.60,
    "ASSUMPTION": 0.35,
    "INFERENCE": 0.25,
}
RECALL_POOL_MIN = 20
RECALL_POOL_FACTOR = 4
SNIPPET_CHARS = 120

_profile_context = threading.local()
_promotion_context = threading.local()


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


# --- identity ----------------------------------------------------------------

# Token pattern for lexical scoring: words, not punctuation, not underscores.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_content(content: str) -> str:
    """Stored form: NFKC, runs of whitespace collapsed to one space, stripped.

    Normalising on write means one fact has one spelling in the database, so a
    tab, a trailing newline or fullwidth digits cannot create a second row.
    """
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", content or "")).strip()


def identity_text(content: str) -> str:
    """Comparison form only; the stored text keeps its original casing."""
    return normalize_content(content).casefold()


def content_id(content: str) -> str:
    """Id is the normalised, case-folded content. Type and provenance excluded:
    the same statement from another source or project is the same record."""
    return sha(identity_text(content))


def _uppercase_count(text: str) -> int:
    return sum(1 for ch in text if ch.isupper())


def _richer_spelling(stored: str, incoming: str) -> str:
    """Prefer the spelling with more capitalisation, ties keep what is stored.

    Comparing by row means the result does not depend on write order for the
    two-way case and never flaps between two writes of the same surface.
    """
    if _uppercase_count(incoming) > _uppercase_count(stored):
        return incoming
    return stored


def _merge_provenance(stored: str, incoming: str) -> str:
    """Comma-joined, order-preserving, deduplicated.

    Provenance accumulates, so merging two observations of one fact keeps both
    names. Re-writing the same name is a no-op, which keeps the column stable
    under repeated saves.
    """
    seen = [p for p in (stored or "").split(",") if p]
    for p in (incoming or "").split(","):
        if p and p not in seen:
            seen.append(p)
    return ",".join(seen)


# --- near-duplicate advisory -------------------------------------------------

# Similarity never merges on its own. Measured scores: real paraphrases of one
# fact land at 0.40-0.64, adversarial near-misses ("payment-service 1000" vs
# "shipping-service 1000") reach 0.82. No single threshold separates those two
# bands, so a high score is an advisory and the only automatic merge requires
# a number-identical candidate above UPSERT_THRESHOLD as well.
SIMILAR_THRESHOLD = 0.50
UPSERT_THRESHOLD = 0.85


def _tokens(text: str) -> list:
    return _TOKEN_RE.findall(identity_text(text))


def _numeric_tokens(text: str) -> frozenset:
    """Numbers are compared exactly: "section 2" and "section 9" are different
    facts however similar the surrounding words are."""
    return frozenset(t for t in _tokens(text) if t.isdigit())


def similarity(a: str, b: str) -> float:
    """Jaccard over word tokens.

    Jaccard, not the overlap coefficient: containment scoring returns 1.0 for
    any text that is a subset of another, which made a short fragment look
    identical to the whole record it came from.
    """
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


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
            _restrict_to_owner(dst)
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
            _restrict_to_owner(tmp)
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
        parent_id   TEXT NOT NULL DEFAULT '',
        project_ids TEXT NOT NULL DEFAULT '',
        last_decay_at TEXT NOT NULL DEFAULT ''
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
        id, content, evidence, tags, tokenize="porter unicode61"
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS records_fts_vocab
        USING fts5vocab(records_fts, row);
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
    CREATE TABLE IF NOT EXISTS promotion_log (
        record_id   TEXT NOT NULL,
        from_type   TEXT NOT NULL,
        to_type     TEXT NOT NULL,
        heat        REAL NOT NULL,
        successes   INTEGER NOT NULL DEFAULT 0,
        sources     TEXT NOT NULL DEFAULT '',
        ts          TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS forgotten_graveyard (
        id          TEXT PRIMARY KEY,
        type        TEXT NOT NULL,
        content     TEXT NOT NULL,
        reason      TEXT NOT NULL DEFAULT '',
        ts          TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_promotion_record ON promotion_log(record_id);
    CREATE INDEX IF NOT EXISTS idx_graveyard_ts     ON forgotten_graveyard(ts);
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

    if version < 3:
        # v3: provenance accumulates on a merge, so one row can name many
        # projects. Existing rows keep their ids; the column defaults to empty
        # and project_id stays the project that first wrote the record.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        if "project_ids" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN project_ids TEXT NOT NULL DEFAULT ''")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (3)")
        conn.commit()

    if version < 4:
        # v4 adds two append-only audit tables. Both are created by
        # _create_schema with IF NOT EXISTS, so this step only records that the
        # upgrade ran; an older database keeps every row it had.
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (4)")
        conn.commit()

    if version < 5:
        # v5: decay needs its own anchor. Without it, applying decay twice
        # re-decays an already-decayed confidence for the same elapsed time,
        # which compounds instead of converging. last_decay_at records when the
        # value was last lowered; existing rows keep their data and begin
        # anchoring on updated_at until the first decay touches them.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        if "last_decay_at" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN last_decay_at TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_version(version) VALUES (5)")
        conn.commit()

    if version < 6:
        # v6: the default FTS tokenizer indexes exact word forms only, so a
        # query for "deplo" or "deploying" misses a record that says "deploy".
        # porter unicode61 stems both sides and still supports prefix queries
        # ("deplo*"). The index is rebuilt from records, so existing rows keep
        # their ids and every recorded word stays searchable.
        conn.execute("DROP TABLE IF EXISTS records_fts_vocab")
        conn.execute("DROP TABLE records_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE records_fts USING fts5("
            "id, content, evidence, tags, tokenize='porter unicode61')"
        )
        conn.execute(
            "INSERT INTO records_fts(id, content, evidence, tags)"
            " SELECT id, content, evidence, tags FROM records"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE records_fts_vocab USING fts5vocab(records_fts, row)"
        )
        _create_fts_triggers(conn)
        conn.execute("DELETE FROM schema_version")
    if version < 7:
        # v7: typo tolerance. The vocabulary table mirrors the FTS index so a
        # query word the index has never seen can be repaired to its nearest
        # term instead of returning nothing. A read-only fts5vocab mirror
        # needs no maintenance; existing databases create it in place.
        has_vocab = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='records_fts_vocab'").fetchone() is not None
        if not has_vocab:
            conn.execute(
                "CREATE VIRTUAL TABLE records_fts_vocab USING fts5vocab(records_fts, row)"
            )
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (7)")
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


def _insert_identity(conn: sqlite3.Connection, record_id: str, rtype: str, norm: str,
                     evidence: str, verified: int, confidence: float, source: str,
                     tags: str, session_id: str, project_id: str, parent_id: str):
    stamp = now_iso()
    conn.execute("""
        INSERT INTO records
            (id,type,content,evidence,verified,confidence,source,tags,
             created_at,updated_at,session_id,project_id,parent_id,project_ids)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (record_id, rtype, norm, evidence or "", 1 if verified else 0, confidence,
          source or "", tags or "", stamp, stamp, session_id or "", project_id or "",
          parent_id or "", project_id or ""))


def _candidates(conn: sqlite3.Connection, content: str, rtype: str, limit: int) -> list:
    """Lexical prefilter: FTS first, LIKE over rows as the fallback.

    FTS only matches tokens that exist, so a reworded fact can score nothing
    there; the LIKE fallback on the longest tokens keeps such a record visible
    to the advisory stage. Both are bounded by a multiple of `limit`, so the
    cost is one query per remember call, not per record.
    """
    own = content_id(content)
    rows = []
    try:
        rows = search(conn, content, limit=limit * 8, rtype=rtype)
    except sqlite3.Error:
        rows = []
    others = [r for r in rows if r["id"] != own]
    if others:
        return others
    # FTS matched nothing but the record itself, so nothing shares a token
    # suffix: widen to any row that contains one of the longest words.
    tokens = sorted(set(_tokens(content)), key=len, reverse=True)[:4]
    if not tokens:
        return []
    sql = "SELECT * FROM records WHERE 1=1"
    params = []
    if rtype:
        sql += " AND type=?"
        params.append(rtype)
    sql += " AND (" + " OR ".join("content LIKE ?" for _ in tokens) + ")"
    params.extend(f"%{tok}%" for tok in tokens)
    sql += " LIMIT ?"
    params.append(limit * 8)
    return [dict(r) for r in conn.execute(sql, params) if r["id"] != own]


def find_similar(conn: sqlite3.Connection, content: str, limit: int = 5,
                 threshold: float = SIMILAR_THRESHOLD, rtype: str = None) -> list:
    """Candidates for one fact, scored by Jaccard, best first.

    Excludes the identical record: asking what a fact resembles does not
    include itself.
    """
    norm = normalize_content(content)
    hits = []
    for row in _candidates(conn, norm, rtype, limit):
        score = round(similarity(norm, row["content"]), 4)
        if score < threshold:
            continue
        hits.append({
            "id": row["id"],
            "type": row["type"],
            "content": row["content"],
            "confidence": row["confidence"],
            "score": score,
            "same_numbers": _numeric_tokens(row["content"]) == _numeric_tokens(norm),
        })
    hits.sort(key=lambda h: (-h["score"], h["id"]))
    return hits[:limit]


def _upsertable(candidate: dict, norm: str) -> bool:
    """Both guards: a high score and identical numbers.

    The score guard keeps genuinely different statements apart; the number
    guard stops a one-character version bump from collapsing into its
    predecessor at a score the threshold alone would accept.
    """
    if float(candidate.get("score") or 0.0) < UPSERT_THRESHOLD:
        return False
    return _numeric_tokens(candidate.get("content") or "") == _numeric_tokens(norm)


def decide(conn: sqlite3.Connection, content: str, rtype: str = "FACT", limit: int = 5,
           threshold: float = SIMILAR_THRESHOLD, upsert: bool = False) -> dict:
    """Read-only: what remembering this content would do.

    `reason` is one of three values and is the only thing that distinguishes a
    merge from a report, so it is contract, not a hint:
      identical  the same content id is already stored; action update
      similar    a candidate exists. With upsert and both guards met this is an
                 explicit merge (action update, advisory says "merging into"),
                 otherwise the write creates a new row and the candidate is
                 only reported (action create, advisory says "similar to")
      novel      nothing scored above the advisory threshold; action create

    Returns action update|create, the id it would touch, the reason above, the
    similar candidates and the human-readable advisory. Never writes.
    """
    norm = normalize_content(content)
    own = content_id(norm)
    if get_record(conn, own):
        return {"action": "update", "id": own, "reason": "identical",
                "similar": [], "advisory": ""}

    similar = find_similar(conn, norm, limit=limit, threshold=threshold, rtype=rtype)
    mergeable = [c for c in similar if upsert and _upsertable(c, norm)]
    if mergeable:
        best = mergeable[0]
        return {"action": "update", "id": best["id"], "reason": "similar",
                "similar": similar,
                "advisory": f"merging into {best['id']} at similarity {best['score']}"}
    if similar:
        best = similar[0]
        return {"action": "create", "id": own, "reason": "similar",
                "similar": similar,
                "advisory": (f"similar to {best['id']} at {best['score']}; "
                             "keep separate unless it is the same fact")}
    return {"action": "create", "id": own, "reason": "novel", "similar": [], "advisory": ""}


def _schema_prior(confidence: float, similar: list) -> float:
    """Bias a new record's initial confidence by schema coherence.

    Coherence is the best (score * existing confidence) among similar records
    that are themselves well-trusted (confidence >= 0.7). Agreement raises the
    prior, contradiction lowers it, never past the caller's own value by more
    than SCHEMA_PRIOR_WEIGHT: the caller's confidence is the base, the schema
    is the nudge (Tse et al. 2007 rapid consolidation; Bartlett 1932 guard:
    a prior never overrides an existing verified record, it only biases the
    new one).
    """
    coherence = 0.5
    for cand in similar:
        if cand.get("confidence", 0.0) < 0.7:
            continue
        coherence = max(coherence, cand["score"] * cand["confidence"])
    return min(1.0, max(0.0, confidence + SCHEMA_PRIOR_WEIGHT * (coherence - 0.5)))


def remember(conn: sqlite3.Connection, rtype: str, content: str, evidence: str = "",
             verified: int = 0, confidence: float = 0.5, source: str = "",
             tags: str = "", session_id: str = "", project_id: str = "",
             parent_id: str = "", upsert: bool = False, limit: int = 5) -> dict:
    """Write by content identity, not by provenance.

    Two spellings of one fact (casing, whitespace) and two provenances of one
    fact (source, project) are the same row; `source` and `project_id` are
    provenance and accumulate, so a merge never loses where a fact was seen.

    Near-duplicates follow `upsert`: off by default, in which case a similar
    record is reported and a new row is written; on, a candidate that clears
    both guards is merged in place. Either way the decision, the candidates and
    the advisory come back to the caller.
    """
    if rtype not in VALID_TYPES:
        raise ValueError(f"invalid type: {rtype}")
    if not (content or "").strip():
        raise ValueError("content must not be empty")
    norm = normalize_content(content)
    confidence = min(1.0, max(0.0, float(confidence)))

    decision = decide(conn, norm, rtype=rtype, limit=limit, upsert=upsert)
    record_id = decision["id"]
    # The merge re-reads the row inside BEGIN IMMEDIATE: decide() is read-only
    # and safe outside, but the merge itself is read-modify-write, and two
    # processes merging different provenance into one row lose the loser's
    # source when both read the same base.
    conn.execute("BEGIN IMMEDIATE")
    existing = get_record(conn, record_id)

    if existing:
        conn.execute(
            "UPDATE records SET content=?, evidence=?, verified=?, confidence=?,"
            " tags=?, session_id=?, parent_id=?, source=?, project_id=?,"
            " project_ids=?, updated_at=?"
            " WHERE id=?",
            (_richer_spelling(existing["content"], norm),
             evidence or existing["evidence"],
             max(1 if verified else 0, existing["verified"]),
             max(confidence, existing["confidence"]),
             tags or existing["tags"],
             session_id or existing["session_id"],
             parent_id or existing["parent_id"],
             _merge_provenance(existing["source"], source),
             _merge_provenance(existing["project_id"], project_id),
             _merge_provenance(existing["project_ids"], project_id),
             now_iso(),
             record_id)
        )
        applied = "updated"
    else:
        prior = _schema_prior(confidence, decision["similar"])
        _insert_identity(conn, record_id, rtype, norm, evidence, verified, prior,
                         source, tags, session_id, project_id, parent_id)
        applied = "inserted"

    conn.commit()
    return {"id": record_id, "type": rtype, "shape": norm, "applied": applied,
            "action": decision["action"], "reason": decision["reason"],
            "similar": decision["similar"], "advisory": decision["advisory"]}


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    A bare MATCH string is a query language: quotes, *, -, NEAR( and boolean
    keywords either raise or silently change meaning. Each word is re-derived
    from the tokenizer's own alphabet, so operator characters are dropped
    rather than escaped, and punctuation-only input collapses to no query.
    The last word carries a prefix marker: typing "deplo" should still find
    "deploy" mid-word, which is how an unfinished thought is typed.
    """
    words = _tokens(query)
    if not words:
        return ""
    quoted = ['"' + w + '"' for w in words[:-1]]
    quoted.append('"' + words[-1] + '"*')
    return " ".join(quoted)


FUZZY_CUTOFF = 0.75
MIN_REPAIR_LEN = 5

def _vocab(conn: sqlite3.Connection) -> list:
    """Distinct stemmed terms in the index, for typo repair.

    fts5vocab is a read-only mirror of the index, so it is always in sync
    and never needs maintenance. Missing table means an uninitialised or
    in-memory database; an empty vocabulary makes every repair a no-op.
    """
    try:
        return [r[0] for r in conn.execute(
            "SELECT term FROM records_fts_vocab ORDER BY term")]
    except sqlite3.OperationalError:
        return []


def _repair_typos(conn: sqlite3.Connection, words: list, vocab: list) -> list:
    """Replace words the index has never seen with their nearest term.

    A word is considered known when it, or anything it prefixes, occurs in
    the index. An unknown word gets the closest vocabulary term above
    FUZZY_CUTOFF, or stays as-is when nothing is close enough, so a
    genuinely novel query word is never rewritten into a wrong match.
    Repairs only run when the exact query returns nothing, which keeps a
    valid query's ranking untouched.
    """
    if not vocab:
        return words
    vocab_set = set(vocab)
    fixed = []
    for w in words:
        if w in vocab_set or any(t.startswith(w) for t in vocab_set):
            fixed.append(w)
            continue
        if len(w) < MIN_REPAIR_LEN:
            fixed.append(w)
            continue
        close = difflib.get_close_matches(w, vocab, n=1, cutoff=FUZZY_CUTOFF)
        fixed.append(close[0] if close else w)
    return fixed


def _search_after_repairs(conn: sqlite3.Connection, query: str, **kwargs) -> list:
    """Search exact first; on an empty result, retry with repaired words."""
    rows = _search_exact(conn, query, **kwargs)
    if rows:
        return rows
    vocab = _vocab(conn)
    words = _repair_typos(conn, _tokens(query), vocab)
    if words != _tokens(query):
        rows = _search_exact(conn, " ".join(words), **kwargs)
    return rows


def _or_query(words: list) -> str:
    """Build an OR match: any word hitting is enough to surface a candidate."""
    return " OR ".join('"' + w + '"*' for w in words)


def _search_relaxed(conn: sqlite3.Connection, query: str, **kwargs) -> list:
    """When no record contains every word, rank by best single-word match.

    A four-word natural-language query ("ganti model pindah sesi") usually
    has no record containing all four, and an AND-only index answers zero
    even though several records cover the intent well. bm25 already ranks
    an OR pool by how strongly each record matches its best term, so the
    top rows are the ones sharing the most/strongest words with the query,
    not random one-word hits. Runs only after the exact AND and the
    typo-repaired retry both return nothing.
    """
    rows = _search_after_repairs(conn, query, **kwargs)
    if rows:
        return rows
    words = _tokens(query)
    if len(words) < 2:
        return []
    or_expr = _or_query(words)
    return _search_exact(conn, ">" + or_expr, **kwargs) if or_expr else []


def _search_exact(conn: sqlite3.Connection, query: str, limit: int = 20, rtype: str = None,
                  project_id: str = None, verified_only: bool = False,
                  min_confidence: float = 0.0, pool: int = None):
    match = query if query.startswith('>') else _fts_query(query)
    if not match:
        return []
    sql = """
        SELECT r.id, r.type, r.content, r.evidence, r.verified, r.confidence, r.source, r.tags,
               r.created_at, r.updated_at, r.session_id, r.project_id, r.parent_id,
               bm25(records_fts) AS rank
        FROM records_fts
        JOIN records r ON r.id = records_fts.id
        WHERE records_fts MATCH ?
    """
    params = [match[1:] if match.startswith(">") else match]
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
    sql += " ORDER BY rank, r.verified DESC, r.confidence DESC, r.updated_at DESC LIMIT ?"
    params.append(pool if pool is not None else limit)
    rows = [dict(row) for row in conn.execute(sql, params)]
    if not rows:
        return []
    ranks = [r["rank"] for r in rows]
    min_rank, max_rank = min(ranks), max(ranks)
    denom = max_rank - min_rank
    for r in rows:
        if denom == 0.0:
            r["lexical"] = 1.0
        else:
            r["lexical"] = 1.0 - (r["rank"] - min_rank) / denom
    return rows


def search(conn: sqlite3.Connection, query: str, limit: int = 20, rtype: str = None,
           project_id: str = None, verified_only: bool = False,
           min_confidence: float = 0.0, pool: int = None):
    """Exact AND first, then typo-repaired, then best single-word match."""
    return _search_relaxed(conn, query, limit=limit, rtype=rtype,
                           project_id=project_id,
                           verified_only=verified_only,
                           min_confidence=min_confidence, pool=pool)

BM25_CEILING = 10.0


def _absolute_lexical(candidates: list) -> dict:
    """bm25 rank on a fixed [0, 1] scale, keyed by record id.

    bm25 is negative (more relevant = more negative) and unbounded, so its
    raw value means nothing across queries, and normalising by the best
    rank in the pool makes every pool's top hit 1.0 regardless of how well
    it actually matched. A fixed ceiling instead: lexical = 1 + rank /
    CEILING, so a perfect match is 1.0, a rank at -CEILING is 0.0, and a
    hit at lexical 0.9 is as strong in one query as in another. Tiny
    corpora produce tiny bm25 magnitudes, so everything clamps near 1.0
    there; that is the honest reading (all hits are near-perfect matches)
    and the spread reappears as the corpus grows.
    """
    return {
        c["id"]: round(min(1.0, max(0.0, 1.0 + c["rank"] / BM25_CEILING)), 6)
        for c in candidates
    }


def recall(conn: sqlite3.Connection, query: str, limit: int = 8, full: bool = False,
           pool: int = None, rtype: str = None, project_id: str = None, now=None,
           verified_only: bool = False, min_confidence: float = 0.0) -> list:
    """Rerank candidate memories combining lexical match, heat and type prior.

    Confidence enters once, through heat (which already weights it 0.45); a
    second direct term would count the same value twice and let a high
    confidence outrank a better lexical match. lexical comes back on an
    absolute [0, 1] scale (bm25 normalised by a fixed ceiling) so a score is
    comparable across queries instead of only within one result pool.
    """
    if not query or not query.strip():
        return []
    pool_size = max(pool or 0, limit * RECALL_POOL_FACTOR, RECALL_POOL_MIN)
    candidates = search(conn, query, limit=pool_size, pool=pool_size,
                        rtype=rtype, project_id=project_id,
                        verified_only=verified_only, min_confidence=min_confidence)
    if not candidates:
        return []
    lexicals = _absolute_lexical(candidates)

    scored = []
    for cand in candidates:
        signals = _record_signals(conn, cand["id"])
        parts = heat_parts(cand, signals, now=now)
        heat = parts["heat"]
        lexical = lexicals[cand["id"]]
        prior = TYPE_PRIOR.get(cand.get("type"), 0.0)
        score = round(
            RECALL_WEIGHTS["lexical"] * lexical
            + RECALL_WEIGHTS["heat"] * heat
            + RECALL_WEIGHTS["type_prior"] * prior,
            6,
        )
        scored.append((cand, parts, heat, lexical, prior, score))

    def _sort_key(item):
        c, parts, heat, lexical, prior, score = item
        ts = _parse_ts(c.get("updated_at"))
        epoch = ts.timestamp() if ts else 0.0
        return (
            -score,
            -int(bool(c.get("verified"))),
            -float(c.get("confidence") or 0.0),
            -epoch,
            str(c.get("id") or ""),
        )

    scored.sort(key=_sort_key)
    top = scored[:limit]

    out = []
    if full:
        for c, parts, heat, lexical, prior, score in top:
            out.append({
                "id": c["id"],
                "type": c["type"],
                "confidence": c["confidence"],
                "verified": c["verified"],
                "score": score,
                "heat": heat,
                "content": c.get("content", ""),
                "evidence": c.get("evidence", ""),
                "lexical": lexical,
                "type_prior": prior,
                "age_days": parts["age_days"],
                "successes": parts["successes"],
            })
    else:
        for c, parts, heat, lexical, prior, score in top:
            norm = normalize_content(c.get("content") or "")
            if len(norm) > SNIPPET_CHARS:
                snippet = norm[:SNIPPET_CHARS - 3] + "..."
            else:
                snippet = norm
            out.append({
                "id": c["id"],
                "type": c["type"],
                "confidence": c["confidence"],
                "verified": c["verified"],
                "score": score,
                "heat": heat,
                "snippet": snippet,
            })
    return out


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


def _parse_ts(value: str):
    """Tolerant ISO parse; a malformed stamp ages to zero rather than crashing."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record_signals(conn: sqlite3.Connection, record_id: str) -> list:
    rows = conn.execute(
        "SELECT signal, source, delta, ts FROM reinforcement WHERE record_id=? ORDER BY ts",
        (record_id,)).fetchall()
    return [dict(r) for r in rows]


def heat_parts(row: dict, signals: list, now=None) -> dict:
    """Decompose heat so every score can be explained and re-derived per record.

    heat = 0.45*confidence + 0.25*recency + 0.30*reinforcement

    recency is computed from `updated_at`, never from confidence. apply_decay
    already lowers confidence as time passes; ageing that value again here
    would count the same elapsed time twice and make an old record look older
    than its own timestamp says.

    Weights are fixed rather than fitted: a fit would encode one corpus's
    behaviour, become unexplainable per record, and drift whenever the corpus
    changes. Fixed weights keep the components comparable across sessions.
    """
    now = now or datetime.now(timezone.utc)
    signals = signals or []
    updated = _parse_ts(row.get("updated_at"))
    age_days = max(0.0, (now - updated).total_seconds() / 86400.0) if updated else 0.0
    recency = 0.5 ** (age_days / HEAT_HALF_LIFE_DAYS)

    successes = [s for s in signals if s.get("signal") == "success"]
    sources = []
    for s in successes:
        name = normalize_content(s.get("source") or "").casefold()
        if name and name not in sources:
            sources.append(name)
    net_delta = sum(float(s.get("delta") or 0.0) for s in signals)
    reinforcement = (0.5 * min(1.0, len(successes) / 2.0)
                     + 0.5 * min(1.0, max(0.0, net_delta) / 0.2))

    confidence = min(1.0, max(0.0, float(row.get("confidence") or 0.0)))
    heat = (HEAT_WEIGHTS["confidence"] * confidence
            + HEAT_WEIGHTS["recency"] * recency
            + HEAT_WEIGHTS["reinforcement"] * reinforcement)
    return {
        "heat": round(min(1.0, max(0.0, heat)), 6),
        "confidence": confidence,
        "age_days": round(age_days, 4),
        "recency": round(recency, 6),
        "reinforcement": round(reinforcement, 6),
        "successes": len(successes),
        "net_delta": round(net_delta, 6),
        "sources": sorted(sources),
    }


def heat_of(conn: sqlite3.Connection, record_id: str, now=None) -> float:
    rec = get_record(conn, record_id)
    if not rec:
        raise ValueError(f"record not found: {record_id}")
    return heat_parts(rec, _record_signals(conn, record_id), now)["heat"]


def last_promotion():
    """The promotion produced by the most recent reinforce() on this thread."""
    return getattr(_promotion_context, "last", None)


def promote_hot(conn: sqlite3.Connection, record_id: str = None, now=None):
    """Turn a hot, twice-corroborated inference into a fact.

    Every condition is required: a promotable type, heat at or above
    PROMOTE_HEAT, and successes reported from two distinct sources after
    normalising case and whitespace. `verified` is left alone: corroboration is
    evidence, not an operator's confirmation.

    Returns the promotion dict, or None when nothing changed, in which case
    nothing is written. With no record_id, every promotable record is examined.
    """
    now = now or datetime.now(timezone.utc)
    if record_id:
        one = get_record(conn, record_id)
        rows = [one] if one else []
    else:
        marks = ",".join("?" for _ in PROMOTABLE_TYPES)
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM records WHERE type IN ({marks})", PROMOTABLE_TYPES)]

    promoted = None
    for row in rows:
        if row["type"] not in PROMOTABLE_TYPES:
            continue
        parts = heat_parts(row, _record_signals(conn, row["id"]), now)
        if parts["heat"] < PROMOTE_HEAT:
            continue
        if len(parts["sources"]) < 2:
            continue
        conn.execute("UPDATE records SET type=?, updated_at=? WHERE id=?",
                     ("FACT", now.isoformat(), row["id"]))
        conn.execute("""
            INSERT INTO promotion_log
                (record_id, from_type, to_type, heat, successes, sources, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (row["id"], row["type"], "FACT", parts["heat"], parts["successes"],
              ",".join(parts["sources"]), now.isoformat()))
        promoted = {"record_id": row["id"], "from_type": row["type"], "to_type": "FACT",
                    "heat": parts["heat"], "successes": parts["successes"],
                    "sources": parts["sources"], "promoted": True}
    if promoted:
        conn.commit()
    return promoted


def forget_cold(conn: sqlite3.Connection, apply: bool = False, now=None,
                floor: float = DECAY_FLOOR, min_age_days: int = FORGET_MIN_AGE_DAYS,
                window_days: int = FORGET_WINDOW_DAYS) -> dict:
    """Find records nothing has touched, and optionally delete them.

    All conditions must hold: unverified, not a RULE or CORRECTION, confidence
    at the decay floor, older than min_age_days since updated_at, and no
    reinforcement inside window_days. The window is bounded by default: any
    reinforcement ever recorded would otherwise protect a record forever, which
    makes forgetting impossible on a corpus that has ever been used. apply=False
    only reports, which is the default: deleting an operator's memory is a
    decision, not a side effect.

    On apply the graveyard row is written before the delete, so a crash between
    the two leaves an audit trail rather than a silently missing record. Only a
    success signal inside the window protects a record: failure and decay lower
    confidence and must not keep a discredited record alive.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=min_age_days)).isoformat()
    window_cutoff = ((now - timedelta(days=window_days)).isoformat()
                     if window_days else None)

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM records WHERE verified=0 AND type NOT IN ('RULE','CORRECTION')"
        " AND confidence <= ? AND updated_at < ?",
        (floor + FORGET_FLOOR_SLACK, cutoff))]

    candidates = []
    for row in rows:
        sql = "SELECT COUNT(*) FROM reinforcement WHERE record_id=? AND signal='success'"
        params = [row["id"]]
        if window_cutoff:
            sql += " AND ts >= ?"
            params.append(window_cutoff)
        if conn.execute(sql, params).fetchone()[0]:
            continue
        candidates.append(row)

    if apply:
        for row in candidates:
            conn.execute(
                "INSERT OR REPLACE INTO forgotten_graveyard (id, type, content, reason, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (row["id"], row["type"], (row["content"] or "")[:200],
                 f"cold: confidence {row['confidence']} at floor, no reinforcement",
                 now.isoformat()))
            conn.execute("DELETE FROM reinforcement WHERE record_id=?", (row["id"],))
            conn.execute("DELETE FROM records WHERE id=?", (row["id"],))
        if candidates:
            conn.commit()
    return {
        "forgotten": len(candidates) if apply else 0,
        "candidates": [r["id"] for r in candidates],
        "apply": bool(apply),
    }


def apply_decay(conn: sqlite3.Connection, now=None, floor: float = DECAY_FLOOR) -> dict:
    """Age every unprotected record's confidence toward `floor`.

    The decay anchor is the later of updated_at and last_decay_at. Anchoring on
    updated_at alone compounds: a second call would decay the already-decayed
    value over the same elapsed time again, so two calls at one instant would
    disagree. Recording the anchor makes the function idempotent for a given
    `now` and still linear across real time.

    Verified records and the correction types are exempt: an operator's
    confirmation and a recorded correction do not become less true with time.

    Returns {record_id: new_confidence} for the records that moved.
    """
    now = now or datetime.now(timezone.utc)
    moved = {}
    rows = [dict(r) for r in conn.execute(
        "SELECT id, confidence, updated_at, last_decay_at, verified, type FROM records")]
    for row in rows:
        if row["verified"] or row["type"] in ("RULE", "CORRECTION"):
            continue
        anchors = [t for t in (_parse_ts(row["updated_at"]),
                              _parse_ts(row["last_decay_at"])) if t]
        if not anchors:
            continue
        anchor = max(anchors)
        age_days = max(0.0, (now - anchor).total_seconds() / 86400.0)
        factor = (1.0 + age_days / DECAY_POWER_TAU_DAYS) ** -DECAY_POWER_BETA
        base = max(floor, float(row["confidence"]))
        target = floor + (base - floor) * factor
        if factor < 0.05:
            target = floor
        value = round(min(float(row["confidence"]), target), 6)
        if value < float(row["confidence"]):
            conn.execute("UPDATE records SET confidence=?, last_decay_at=? WHERE id=?",
                         (value, now.isoformat(), row["id"]))
            moved[row["id"]] = value
    if moved:
        conn.commit()
    return moved


def reinforce(conn: sqlite3.Connection, record_id: str, signal: str,
              delta: float = None, source: str = "") -> float:
    """Apply a reinforcement signal, then reconsider promotion on success.

    An explicit delta overrides the canonical one. Promotion runs only for a
    success signal, and its outcome is reported through last_promotion() so the
    caller can say what happened without re-reading the row.

    Negative signals (failure, decay) do not touch updated_at: a record a
    session just discredited must stay eligible for forgetting, not be
    refreshed by the very signal that lowered it.
    """
    if signal not in VALID_SIGNALS:
        raise ValueError(f"invalid signal: {signal}")

    row = conn.execute("SELECT confidence FROM records WHERE id=?",
                       (record_id,)).fetchone()
    if row is None:
        raise ValueError(f"record not found: {record_id}")

    actual_delta = float(delta) if delta is not None else SIGNAL_DELTAS[signal]
    # Atomic clamp in SQL: concurrent reinforces each read the same base, so a
    # Python-side read-modify-write loses every signal but the last.
    if actual_delta >= 0:
        conn.execute(
            "UPDATE records SET confidence=min(1.0, max(0.0, confidence+?)), updated_at=? WHERE id=?",
            (actual_delta, now_iso(), record_id))
    else:
        conn.execute(
            "UPDATE records SET confidence=min(1.0, max(0.0, confidence+?)) WHERE id=?",
            (actual_delta, record_id))
    new_conf = conn.execute("SELECT confidence FROM records WHERE id=?",
                            (record_id,)).fetchone()[0]
    conn.execute("""
        INSERT INTO reinforcement (record_id, signal, delta, source, ts)
        VALUES (?, ?, ?, ?, ?)
    """, (record_id, signal, actual_delta, source, now_iso()))
    conn.commit()

    _promotion_context.last = None
    if signal == "success":
        _promotion_context.last = promote_hot(conn, record_id)
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
    result["promotions"] = conn.execute("SELECT COUNT(*) AS n FROM promotion_log").fetchone()["n"]
    result["forgotten"] = conn.execute("SELECT COUNT(*) AS n FROM forgotten_graveyard").fetchone()["n"]
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


# --- Markdown Mirror & Sync --------------------------------------------------

def _format_markdown_record(r: dict) -> str:
    parts = [f"## {r['id']}"]
    parts.append(f"- confidence: {r['confidence']}")
    parts.append(f"- verified: {r['verified']}")
    if r.get("source"):
        parts.append(f"- sources: {r['source']}")
    parts.append("")
    parts.append(r["content"])
    if r.get("evidence"):
        for ev_line in str(r["evidence"]).splitlines():
            parts.append(f"> {ev_line}")
    parts.append("")
    return "\n".join(parts) + "\n"


def _git_run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.strip() if exc.stderr else exc.stdout.strip()
        raise RuntimeError(err) from exc


def export_markdown(conn: sqlite3.Connection, out_dir, types=None) -> dict:
    """Export memory records to deterministic Markdown files with a manifest."""
    out_path = Path(out_dir)
    if not out_path.exists():
        raise FileNotFoundError(f"Output directory does not exist: {out_dir}")
    if not out_path.is_dir():
        raise NotADirectoryError(f"Output path is not a directory: {out_dir}")
    out_path = out_path.resolve()

    for entry in out_path.iterdir():
        if entry.is_symlink():
            try:
                entry.resolve().relative_to(out_path)
            except ValueError:
                raise ValueError(f"Symlink escapes output directory: {entry}")

    if types is not None:
        target_types = sorted(types)
    else:
        cur = conn.execute("SELECT DISTINCT type FROM records WHERE type IS NOT NULL")
        existing_types = {row[0] for row in cur.fetchall()}
        target_types = [t for t in sorted(VALID_TYPES) if t in existing_types]

    new_files = {}
    index_entries = []

    for rtype in target_types:
        cur = conn.execute(
            "SELECT id, type, content, confidence, verified, source, evidence "
            "FROM records WHERE type = ? ORDER BY id ASC",
            (rtype,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        filename = f"{rtype.lower()}.md"
        body = f"# {rtype}\n\n" + "".join(_format_markdown_record(r) for r in rows)
        new_files[filename] = body
        index_entries.append(f"- [{rtype}]({filename}) - {len(rows)} record")

    index_body = "# Memory Index\n\n"
    if index_entries:
        index_body += "\n".join(index_entries) + "\n"
    new_files["INDEX.md"] = index_body

    for relpath in new_files:
        target_p = out_path / relpath
        if target_p.is_symlink():
            try:
                target_p.resolve().relative_to(out_path)
            except ValueError:
                raise ValueError(f"Symlink escapes output directory: {target_p}")
        try:
            target_p.resolve().relative_to(out_path)
        except ValueError:
            raise ValueError(f"Target path escapes output directory: {target_p}")

    manifest_path = out_path / ".honcho-mirror.json"
    old_manifest_files = {}
    if manifest_path.is_file():
        try:
            old_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(old_data, dict) and isinstance(old_data.get("files"), dict):
                old_manifest_files = old_data["files"]
        except Exception:
            old_manifest_files = {}

    new_manifest_files = {
        relpath: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for relpath, content in new_files.items()
    }

    removed = []
    stale_relpaths = set(old_manifest_files.keys()) - set(new_manifest_files.keys())
    for rel in sorted(stale_relpaths):
        stale_path = out_path / rel
        try:
            stale_resolved = stale_path.resolve()
            stale_resolved.relative_to(out_path)
        except ValueError:
            continue
        if stale_path.is_file() or stale_path.is_symlink():
            stale_path.unlink()
            removed.append(rel)

    written = []
    unchanged = []
    for relpath in sorted(new_files.keys()):
        content_bytes = new_files[relpath].encode("utf-8")
        target = out_path / relpath
        if target.is_file() and not target.is_symlink():
            if target.read_bytes() == content_bytes:
                unchanged.append(relpath)
                continue
        target.write_bytes(content_bytes)
        written.append(relpath)

    new_manifest_data = {
        "version": 1,
        "files": dict(sorted(new_manifest_files.items())),
    }
    manifest_bytes = (json.dumps(new_manifest_data, indent=2, sort_keys=True) + "\n").encode("utf-8")

    if not written and not removed and manifest_path.is_file():
        pass
    else:
        if not (manifest_path.is_file() and manifest_path.read_bytes() == manifest_bytes):
            manifest_path.write_bytes(manifest_bytes)

    return {"written": written, "removed": removed, "unchanged": unchanged, "out_dir": str(out_path)}


def sync_mirror(out_dir, message: str = None, init: bool = False) -> dict:
    """Sync markdown mirror directory to a dedicated git repository without pushing."""
    out_path = Path(out_dir)
    if not out_path.exists():
        raise FileNotFoundError(f"Output directory does not exist: {out_dir}")
    if not out_path.is_dir():
        raise NotADirectoryError(f"Output path is not a directory: {out_dir}")
    out_path = out_path.resolve()

    module_dir = Path(__file__).resolve().parent
    if out_path == module_dir or module_dir.is_relative_to(out_path):
        raise ValueError(f"Module directory {module_dir} is inside output directory {out_path}")

    proc_top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=out_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc_top.returncode == 0:
        top_level = Path(proc_top.stdout.strip()).resolve()
        if top_level != out_path:
            raise ValueError(f"Git toplevel {top_level} differs from {out_path}")
    else:
        if not init:
            raise ValueError(f"Output directory is not a git repository: {out_dir}")
        _git_run(["git", "init"], cwd=out_path)
        proc_top = _git_run(["git", "rev-parse", "--show-toplevel"], cwd=out_path)
        top_level = Path(proc_top.stdout.strip()).resolve()
        if top_level != out_path:
            raise ValueError(f"Git toplevel {top_level} differs from {out_path}")

    manifest_file = out_path / ".honcho-mirror.json"
    if not manifest_file.is_file():
        raise FileNotFoundError(f"Manifest not found in {out_path}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    files_to_stage = sorted(manifest.get("files", {}).keys())

    for rel in files_to_stage:
        target = (out_path / rel).resolve()
        try:
            target.relative_to(out_path)
        except ValueError:
            raise ValueError(f"Target file escapes output directory: {rel}")

    if files_to_stage:
        _git_run(["git", "add", "--"] + files_to_stage, cwd=out_path)

    diff_proc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=out_path)
    if diff_proc.returncode == 0:
        return {"committed": False, "reason": "clean"}
    if diff_proc.returncode != 1:
        raise RuntimeError(f"git diff --cached failed with code {diff_proc.returncode}")

    commit_msg = message or "chore(memory): sync markdown mirror"
    _git_run(["git", "commit", "-m", commit_msg], cwd=out_path)
    sha_proc = _git_run(["git", "rev-parse", "HEAD"], cwd=out_path)
    return {"committed": True, "sha": sha_proc.stdout.strip(), "files": files_to_stage}


if __name__ == "__main__":
    conn = connect()
    init_db(conn)
    print(json.dumps({
        "stats": stats(conn),
        "health": health(conn),
    }, indent=2))
    conn.close()
