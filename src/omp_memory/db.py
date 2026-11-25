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
import math
import sqlite3
import time
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

def env_int(name: str, default: int, low: int = 0, high: int = 2 ** 31 - 1) -> int:
    """Read an integer env var without ever raising.

    These values are read at import time, so a typo like
    OMP_MEMORY_BUSY_TIMEOUT_MS=30s used to abort the import of the whole
    package -- the MCP server would not start at all, which reads to the
    operator as "the tool is broken", not "one setting is wrong". A bad
    value falls back to the default and is clamped to a sane range.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(str(raw).strip(), 10)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


DB_PATH = os.environ.get("OMP_MEMORY_DB", str(Path.home() / ".omp/memory/memory.db"))
BACKUP_DIR = os.environ.get("OMP_MEMORY_BACKUP_DIR", str(Path.home() / ".omp/memory/backups"))
MAX_BACKUPS = env_int("OMP_MEMORY_MAX_BACKUPS", 10, 0, 1000)
BUSY_TIMEOUT_MS = env_int("OMP_MEMORY_BUSY_TIMEOUT_MS", 30000, 1, 600000)


def _restrict_to_owner(path: str) -> None:
    """Best-effort 0600. POSIX honours it; Windows has no such mode bits and chmod only touches
    the read-only flag, so a failure or a no-op there must not abort a backup or a repair."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


VALID_TYPES = {"FACT", "ASSUMPTION", "INFERENCE", "DECISION", "RULE", "CORRECTION",
               "INTENT", "WORKFLOW", "PROCEDURAL", "REFLECTIVE"}
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

# Heat recency rides the same power law as decay (see heat_parts).
HEAT_DECAY_POWER_TAU_DAYS = DECAY_POWER_TAU_DAYS
HEAT_DECAY_POWER_BETA = DECAY_POWER_BETA
# Spacing effect (Cepeda et al. 2006/2008): a repeat right after the last one
# is nearly worthless; the same repeat after a gap is what consolidates.
# The horizon is DECAY_POWER_TAU_DAYS: reinforcing on the scale of the
# retention curve is what keeps a record on it.
SPACING_MIN_GAP_HOURS = 1.0
SPACING_FULL_GAP_HOURS = 24.0
# Per-record memory strength (Duolingo HLR, Settles & Meeder 2016; MemoryBank
# 2305.10250): a successful recall at a spaced gap lengthens the record's own
# forgetting horizon; failure shortens it. decay_tau stores the personal tau
# in days (0 = global curve). Growth is multiplicative with a cap so one hot
# streak cannot make a record immortal; the floor keeps failure meaningful.
STRENGTH_GROWTH = 1.5
STRENGTH_SHRINK = 0.7
STRENGTH_TAU_MAX_DAYS = 365.0
STRENGTH_TAU_MIN_DAYS = 2.0
# Retrieval-induced forgetting (Anderson, Bjork & Bjork 1994; Murayama et
# al. 2014): retrieving some memories actively inhibits competing ones. A
# CORRECTION that supersedes an older record, or a retrieval that picks a
# winner out of a cluster of similar records, penalizes the loser by
# RIF_PENALTY. The penalty is ordinary confidence afterwards: inhibition
# wears off, and the record can still recover through reinforcement.
RIF_PENALTY = 0.15
RIF_MIN_CONFIDENCE = 0.7
# Retrieval practice (Roediger & Karpicke 2006): a successful retrieval is
# itself a learning event and beats restudying. Every recall that returns a
# record logs a zero-delta 'retrieval' signal; it moves nothing by itself
# but starts the spacing clock and feeds the consolidation pass. There,
# a record retrieved three or more times in the last week without a
# success signal earns CONSOLIDATE_GAIN: repeated retrieval without
# contradiction is evidence of stability.
RETRIEVAL_WINDOW_DAYS = 7
RETRIEVALS_FOR_CONSOLIDATION = 3
CONSOLIDATE_GAIN = 0.05
# Abstention (Chen & Liu 2018, "Recall Is Not What You Need"): a reranked
# score below this floor means nothing in the pool actually answers the
# query; the caller gets abstained=True instead of a confident-looking
# list of near-misses.
ABSTAIN_SCORE = 0.5
FORGET_FLOOR_SLACK = 0.01
FORGET_MIN_AGE_DAYS = 180
FORGET_WINDOW_DAYS = 365
# Offline consolidation (sleep-time compute, Lin et al. 2025): the pass runs
# decay, consolidation, pruning and eviction together, and evicts only when a
# project exceeds the budget. Budget is per project_id; unscoped records live
# under the empty-string project and are never evicted.
SLEEP_EVICT_BUDGET = 500
SLEEP_PRUNE_SIMILARITY = 0.80
# Conservative deletion (rate-distortion, A3): a merge is recoverable
# (the keeper still holds the fact) but a delete is not, so eviction
# requires stronger evidence of redundancy than a merge does.
# Hypercorrection (B2): correcting a high-confidence record produces a
# larger, more lasting update than correcting a doubtful one - the
# surprise gap drives the rewrite. Applied on reinforce(correction) when
# the record's confidence was already high, with a decay exemption so the
# correction is not immediately cooled back down.
HYPERCORRECTION_MIN_CONFIDENCE = 0.7
HYPERCORRECTION_FACTOR = 1.5
HYPERCORRECTION_DECAY_GRACE_DAYS = 30.0
# Event segmentation (B6): records written inside one event boundary share
# an event_id, so temporal joins and scoped replay can follow the event
# structure of a session instead of only its clock.
RECONSOLIDATION_WINDOW_MINUTES = 45.0
# Reconsolidation gate (Sevenster 2013; Lee et al. 2017): only a correction
# that contradicts what the record says opens the content-rewrite window.
# A success recall is read-only reinforcement, never a rewrite. The rewrite
# also demands trust: Metcalfe & Eich 2019 showed a wrong correction to a
# confidently-wrong record contaminates it deeper, so the caller must mark
# the correction as verified or the rewrite is refused.
REWRITE_MAX_CONTENT_LEN = 20000
# Superseded records keep their row (history is append-only) but are
# downranked in recall: the correction that replaced them is the current
# answer. A small penalty is enough because the replacement is usually
# the lexical match, not the loser; the replacement itself earns a small
# boost, so a correction outranks its stale twin rather than merely
# trailing it. Chain resolution lives on the write path: when a winner
# itself is superseded, every predecessor follows the new head, so a
# query always resolves a chain to its current answer.
SUPERSEDED_BOOST = 0.05
SUPERSEDED_PENALTY = 0.15
# Low-trust provenance. An auto-captured or scraped record competes with a
# human-verified one on equal footing unless something marks the difference.
# The penalty is deliberately small and only applies when the record is
# unverified AND its source is in the low-trust set: a big or unconditional
# penalty would downrank genuine auto-captured knowledge, which is most of a
# real corpus. Measured, not assumed: bench/provenance_ab.py.
PROVENANCE_PENALTY_ENABLED = True
PROVENANCE_PENALTY = 0.04
LOW_TRUST_SOURCES = frozenset({
    "auto-capture", "scrape", "scraped", "external", "web", "unknown",
})
# Cue-triggered recall for INTENT records (Rummel & Kvavilashvili 2023): a
# query carrying the intent's cue pulls the intent forward by this bonus,
# scaled by importance and faded by how long the intent has waited.
INTENT_CUE_BONUS = 0.10
# Intent decay tau (days): fulfillment probability fades like 1/(1+age/tau)
# (Rummel 2023 reports intentions lose ~.12 force per delay unit).
INTENT_DECAY_TAU_DAYS = 7.0

PRE_COMPRESS_CHECKPOINT_API_VERSION = 2
SCHEMA_VERSION = 18
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
    "WORKFLOW": 0.65,
    "INTENT": 0.50,
    "PROCEDURAL": 0.55,
    "REFLECTIVE": 0.45,
    "ASSUMPTION": 0.35,
    "INFERENCE": 0.25,
}
RECALL_POOL_MIN = 20
# bm25 is type-blind: a tentative INFERENCE and a hard-won RULE matching the
# same words rank identically. Weight the bm25 rank by type so binding records
# surface above speculation on a lexical tie. Weight divides the (negative)
# rank, so a larger weight = smaller rank = earlier in the ORDER BY.
TYPE_BM25_WEIGHTS = {
    "RULE": 1.4,
    "CORRECTION": 1.4,
    "WORKFLOW": 1.2,
    "DECISION": 1.2,
    "INTENT": 1.0,
    "PROCEDURAL": 1.2,
    "REFLECTIVE": 0.9,
    "FACT": 1.0,
    "INFERENCE": 0.8,
    "ASSUMPTION": 0.7,
}
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


def normalize_cue(cue: str) -> str:
    """Cue form: the normalized content, lowercased.

    Cues are matched against query tokens, which the tokenizer already
    lowercases, so a cased cue would need a second casefold on every read.
    """
    return normalize_content(cue).lower()


def content_id(content: str) -> str:
    """Id is the normalised, case-folded content. Type and provenance excluded:
    the same statement from another source or project is the same record.

    A correction rewrite (reinforce, signal=correction) is the one operation
    that deliberately breaks this binding: the row keeps its id while its
    content moves to the corrected statement. Callers that need id-content
    correspondence should read the row, not recompute content_id."""
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
# SQLite's LIKE implementation has a pattern-complexity ceiling; tokens
# longer than this are truncated for the LIKE prefilter only.
MAX_LIKE_TOKEN = 64
SIMILAR_THRESHOLD = 0.50
UPSERT_THRESHOLD = 0.85


def _tokens(text: str) -> list:
    return _TOKEN_RE.findall(identity_text(text))


_NUMERIC_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:"
    r"seconds|secs|sec|minutes|mins|min|hours|hrs|hr|days|day|weeks|wk|months|years|yr"
    r"|ms|tb|gb|mb|kb|px|rem|em|%|b|k|m|s|h|d|w|y"
    r")?",
    re.IGNORECASE)


def _numeric_tokens(text: str) -> frozenset:
    """Numbers are compared exactly, including their unit.

    "timeout 1000ms", "retention 30d", "$40k", "5GB" all reduce to nothing
    under a bare isdigit() test, which silently disabled the number guard on
    both the upsert path and the numeric-conflict branch. The unit is kept in
    the token so "30 days" and "30 hours" cannot be treated as the same
    quantity; whitespace between number and unit is normalised away.
    """
    out = set()
    for m in _NUMERIC_RE.finditer(text):
        t = re.sub(r"\s+", "", m.group(0).lower())
        if not any(ch.isdigit() for ch in t):
            continue
        # Plural/long unit spellings normalise to one canonical form so
        # "30 days" and "30d" are the same quantity, while "30d" and "30h"
        # stay different.
        t = re.sub(r"(seconds|secs|sec)$", "s", t)
        t = re.sub(r"(minutes|mins|min)$", "min", t)
        t = re.sub(r"(hours|hrs|hr)$", "h", t)
        t = re.sub(r"(days|day)$", "d", t)
        t = re.sub(r"(weeks|wk)$", "w", t)
        t = re.sub(r"(months)$", "mo", t)
        t = re.sub(r"(years|yr)$", "y", t)
        out.add(t)
    return frozenset(out)


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


def repair_from_backup(db_path: str | None = None) -> bool:
    """Restore db_path from the newest usable backup. Never deletes db_path.

    VACUUM INTO reads a source database and writes a compacted image, so the
    backup is the source and a temporary file is the destination. The target is
    then replaced atomically, so a failure mid-repair cannot leave the database
    missing.
    """
    db_path = db_path or os.environ.get("OMP_MEMORY_DB", DB_PATH)
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

def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection. Raises on failure; the file is never removed."""
    db_path = db_path or os.environ.get("OMP_MEMORY_DB", DB_PATH)
    ensure_dirs()
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    # secure_delete: a forgotten or redacted row must not stay readable in
    # free SQLite pages. Measured cost: ~10ms per 1000 deleted rows.
    conn.execute("PRAGMA secure_delete=ON")
    return conn


def store_version(conn: sqlite3.Connection) -> int:
    """Change counter for cache invalidation across processes.

    PRAGMA data_version increments when ANOTHER connection commits, so a
    long-lived reader (web viewer, MCP server) can detect writes made by
    the capture timer or a CLI call without polling row counts. Own writes
    on this connection do not bump it; callers that also write through the
    same connection should invalidate their caches at the write site.
    """
    return conn.execute("PRAGMA data_version").fetchone()[0]


def integrity_check(conn: sqlite3.Connection) -> bool:
    return _run_check(conn, "integrity_check")


def _has_new_columns(conn: sqlite3.Connection) -> bool:
    """Do the v11/v12 columns exist (migration may not have run yet)?"""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()}
    except sqlite3.DatabaseError:
        return False
    return {"importance", "cue", "superseded_by"} <= cols


def _run_check(conn: sqlite3.Connection, pragma: str) -> bool:
    """Run one integrity pragma; only real corruption counts as failure.

    Two failure shapes here are contention, not a corrupt file:
    - "database is locked"/"busy": a concurrent writer holds the lock.
    - "malformed inverted index": an FTS5 shadow table is mid-write by
      another connection. quick_check's scan is not snapshot-safe across a
      concurrent FTS transaction even in WAL, so the verdict is transient.
      It is retried, then arbitrated by fts5's own integrity-check command,
      which is the canonical verdict for the index.
    """
    for attempt in range(8):
        try:
            row = conn.execute(f"PRAGMA {pragma}").fetchone()
            value = str(row[0]) if row else "ok"
            if value == "ok":
                return True
            if "malformed" in value:
                if attempt < 7:
                    time.sleep(0.1)
                    continue
                conn.execute(
                    "INSERT INTO records_fts(records_fts) VALUES('integrity-check')")
                return True
            return False
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                return False
            time.sleep(0.02 * (attempt + 1))
        except sqlite3.DatabaseError:
            return False
    return False


def quick_check(conn: sqlite3.Connection) -> bool:
    return _run_check(conn, "quick_check")


# --- schema ------------------------------------------------------------------

_RECORD_COLUMNS = (
    "id", "type", "content", "evidence", "verified", "confidence", "source",
    "tags", "created_at", "updated_at", "session_id", "project_id", "parent_id",
    "project_ids", "last_decay_at", "about_peer", "event_id",
    "decay_exempt_until", "superseded_by",
)
_RECORDS_DDL = """CREATE TABLE IF NOT EXISTS records (
        id          TEXT PRIMARY KEY,
        type        TEXT NOT NULL CHECK (type IN ('FACT','ASSUMPTION','INFERENCE','DECISION','RULE',
                                                  'CORRECTION','INTENT','WORKFLOW','PROCEDURAL','REFLECTIVE')),
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
        last_decay_at TEXT NOT NULL DEFAULT '',
        about_peer TEXT NOT NULL DEFAULT '',
        event_id TEXT NOT NULL DEFAULT '',
        decay_exempt_until TEXT NOT NULL DEFAULT '',
        superseded_by TEXT NOT NULL DEFAULT '',
        importance REAL NOT NULL DEFAULT 0.5,
        cue TEXT NOT NULL DEFAULT '',
        decay_tau REAL NOT NULL DEFAULT 0,
        valid_from TEXT NOT NULL DEFAULT '',
        valid_until TEXT NOT NULL DEFAULT '',
        anchor_path TEXT NOT NULL DEFAULT '',
        anchor_sha TEXT NOT NULL DEFAULT '',
        stale INTEGER NOT NULL DEFAULT 0,
        consolidated_at TEXT NOT NULL DEFAULT ''
    );"""


def _create_schema(conn: sqlite3.Connection):
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL
    );
    {_RECORDS_DDL}
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
        signal        TEXT NOT NULL CHECK (signal IN ('success','failure','correction',
                                                      'reinforce','decay','retrieval')),
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
    CREATE TABLE IF NOT EXISTS pending_writes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rtype TEXT NOT NULL,
        content TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0.35,
        source TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '',
        event_id TEXT NOT NULL DEFAULT '',
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        decided_at TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS not_conflict (
        a TEXT NOT NULL,
        b TEXT NOT NULL,
        ts TEXT NOT NULL,
        PRIMARY KEY (a, b)
    );
    CREATE INDEX IF NOT EXISTS idx_promotion_record ON promotion_log(record_id);
    CREATE INDEX IF NOT EXISTS idx_graveyard_ts     ON forgotten_graveyard(ts);
    """)


def _create_fts_triggers(conn: sqlite3.Connection):
    """(Re)create the FTS sync triggers.

    Called on every open by _reconcile_fts. When the three triggers already
    exist the recreation is skipped: DROP+CREATE runs statement-by-statement
    in autocommit, so a concurrent connection can observe the triggers
    half-dropped (CREATE fails with "already exists" once another thread
    re-adds them, "no such table" if a swap lands between the two halves).
    The migration paths that legitimately replace the table drop them first.
    """
    # Existence is not enough: an older build keyed these triggers on the
    # `id` column, and a DELETE ... WHERE id = ? makes FTS5 scan the whole
    # content table (14k statements for one update at 14k rows, 264ms per
    # write). The body is inspected so a database created by the old build
    # gets the rowid-keyed triggers on its next open.
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger'"
        " AND name IN ('records_ai','records_ad','records_au')"
    ).fetchall()
    if len(rows) == 3 and all("rowid" in (r[0] or "") for r in rows):
        return
    conn.executescript("""
    DROP TRIGGER IF EXISTS records_ai;
    DROP TRIGGER IF EXISTS records_ad;
    DROP TRIGGER IF EXISTS records_au;
    CREATE TRIGGER records_ai AFTER INSERT ON records BEGIN
        INSERT INTO records_fts(rowid, id, content, evidence, tags)
        VALUES (new.rowid, new.id, new.content, new.evidence, new.tags);
    END;
    CREATE TRIGGER records_ad AFTER DELETE ON records BEGIN
        DELETE FROM records_fts WHERE rowid = old.rowid;
    END;
    CREATE TRIGGER records_au AFTER UPDATE ON records BEGIN
        DELETE FROM records_fts WHERE rowid = old.rowid;
        INSERT INTO records_fts(rowid, id, content, evidence, tags)
        VALUES (new.rowid, new.id, new.content, new.evidence, new.tags);
    END;
    """)



def _rebuild_records_check(conn: sqlite3.Connection) -> bool:
    """Rebuild `records` when its CHECK constraint lacks a valid type.

    SQLite cannot ALTER a CHECK, so adding a record type means rebuilding
    the table. A new table is created, rows carry over, and the old table
    is dropped. The rename would make FKs point at `records_old` and drop
    the FTS triggers, so this is a new-table swap that keeps the name
    `records`; `_reconcile_fts` recreates the trigger state afterwards.
    The swap is one transaction: dying halfway would leave records
    half-swapped and _migrate would never retry it. foreign_keys cannot
    change inside a transaction, so the pragma calls bracket it.
    """
    check = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
    ).fetchone()[0]
    stale = bool(check) and any(
        f"'{t}'" not in check for t in sorted(VALID_TYPES)
    )
    if not stale:
        return False
    conn.execute("PRAGMA foreign_keys=off")
    conn.execute("BEGIN")
    try:
        conn.execute(_RECORDS_DDL.replace(
            "CREATE TABLE IF NOT EXISTS records", "CREATE TABLE records_new"))
        carry = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        shared = ",".join(c for c in carry if c in _RECORD_COLUMNS)
        conn.execute(f"INSERT INTO records_new ({shared})"
                     f" SELECT {shared} FROM records")
        conn.execute("DROP TABLE records")
        conn.execute("ALTER TABLE records_new RENAME TO records")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=on")
    return True

def _current_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _reconcile_fts(conn: sqlite3.Connection):
    """Self-heal the FTS index and plain indexes on `records`.

    Runs on every open, before the version gate: a database can reach
    SCHEMA_VERSION with a broken FTS index (a migration that crashed
    between the table swap and the refill, or the pre-hardened
    rename-based rebuild) and the version gate would never repair it.
    Trigger recreation and index creation are idempotent; the refill
    only fires when the index disagrees with the table, so a healthy
    database pays two counts and no writes.
    """
    _create_fts_triggers(conn)
    for name, col in (("idx_records_type", "type"),
                      ("idx_records_project", "project_id"),
                      ("idx_records_session", "session_id"),
                      ("idx_records_parent", "parent_id"),
                      ("idx_records_verified", "verified"),
                      ("idx_records_confidence", "confidence")):
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON records({col})")
    fts_count = conn.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0]
    rec_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    if fts_count != rec_count:
        conn.execute("DELETE FROM records_fts")
        conn.execute(
            "INSERT INTO records_fts(rowid, id, content, evidence, tags)"
            " SELECT rowid, id, content, evidence, tags FROM records")


def _migrate(conn: sqlite3.Connection):
    """Bring an older database up to SCHEMA_VERSION. Idempotent.

    _reconcile_fts runs on BOTH paths. It used to run only on the
    already-current early return, so a legacy database that actually went
    through the migration blocks (v14 -> v18 and every other upgrade) ended
    up with an empty FTS index: quick_check passed, every row was present,
    and recall returned nothing. Reproduced with a synthetic v14 database:
    30 rows, 0 FTS rows, recall 0 hits.
    """
    version = _current_schema_version(conn)
    if version >= SCHEMA_VERSION:
        _reconcile_fts(conn)
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
                "INSERT INTO records_fts(rowid, id, content, evidence, tags)"
                " SELECT rowid, id, content, evidence, tags FROM records"
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
            "INSERT INTO records_fts(rowid, id, content, evidence, tags)"
            " SELECT rowid, id, content, evidence, tags FROM records"
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

    if version < 8:
        # v8: retrieval practice (Roediger & Karpicke 2006). The signal CHECK
        # gains 'retrieval', logged by recall with delta 0. SQLite cannot
        # ALTER a CHECK, so the table is rebuilt and rows carried over.
        conn.execute("PRAGMA foreign_keys=off")
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE reinforcement RENAME TO reinforcement_old")
        conn.execute(
            """CREATE TABLE reinforcement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL,
                signal TEXT NOT NULL CHECK (signal IN
                    ('success','failure','correction','reinforce','decay','retrieval')),
                delta REAL NOT NULL DEFAULT 0.1,
                source TEXT NOT NULL DEFAULT '',
                ts TEXT NOT NULL,
                FOREIGN KEY (record_id) REFERENCES records(id)
            )""")
        conn.execute(
            "INSERT INTO reinforcement (id, record_id, signal, delta, source, ts)"
            " SELECT id, record_id, signal, delta, source, ts FROM reinforcement_old")
        conn.execute("DROP TABLE reinforcement_old")
        conn.execute("PRAGMA foreign_keys=on")
        conn.execute("COMMIT")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (8)")

    if version < 9:
        # v9: peer-scoped memory. `about_peer` marks whose facts a row is
        # about (nullable, default '' = operator's own). Filtering happens
        # in search, so a query can stay inside one peer's context.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        if cols and "about_peer" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN about_peer TEXT NOT NULL DEFAULT ''")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (9)")
        conn.commit()

    if version < 10:
        # v10: event segmentation (B6) and decay grace (B2). `event_id`
        # groups records written inside one event boundary so temporal
        # joins and scoped replay follow event structure, not just the
        # clock. `decay_exempt_until` shields a hypercorrected record
        # from decay for a grace period: the correction must be allowed
        # to settle before the forgetting curve applies again.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        if cols and "event_id" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN event_id TEXT NOT NULL DEFAULT ''")
        if cols and "decay_exempt_until" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN decay_exempt_until TEXT NOT NULL DEFAULT ''")
        _rebuild_records_check(conn)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (10)")
        conn.commit()
        # The swap dropped the FTS triggers with the old records table;
        # recreate the index state before any write can land. On the
        # fresh-migration path the refill is a no-op (counts already
        # agree), so this only pays on the broken path.
        _reconcile_fts(conn)

    if version < 11:
        # v11: supersede links. `superseded_by` points a superseded record at
        # the CORRECTION that replaced it, so recall can downrank it and a
        # caller can follow the chain instead of reading a stale fact as
        # current. Rewrites stay in the row history via the reinforcement log.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        if cols and "superseded_by" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN superseded_by TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_superseded ON records(superseded_by)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (11)")
        conn.commit()

    if version < 12:
        # v12: intent fields. `cue` is the environment signal that should
        # trigger the intent (Rummel & Kvavilashvili 2023); `importance`
        # scales the recall bonus so an urgent intent outranks a casual
        # one when both cues appear in one query.
        # ALTER TABLE ADD COLUMN with a NOT NULL DEFAULT leaves the column
        # NULL on existing rows in this SQLite build: quick_check then
        # reports "NULL value in records.importance" even though a SELECT
        # shows the default. An explicit UPDATE backfills the real cells
        # and clears the report.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        if cols and "importance" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN importance REAL NOT NULL DEFAULT 0.5")
        if cols and "cue" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN cue TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE records SET importance=0.5 WHERE importance IS NULL")
        conn.execute("UPDATE records SET cue='' WHERE cue IS NULL")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (12)")
        conn.commit()

    if version < 13:
        # v13: WORKFLOW record type (AMW 2409.07429, ExpeL 2308.10144).
        # Step-by-step procedures the operator confirmed working, distinct
        # from a DECISION (why) and a FACT (what): the workflow is the how.
        # Databases that already reached v12 skip the v10 block, so the
        # CHECK rebuild (SQLite cannot ALTER a CHECK) runs here too: the
        # guard checks the live CHECK, not the schema version.
        if _rebuild_records_check(conn):
            _reconcile_fts(conn)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (13)")
        conn.commit()

    if version < 13:
        # v13: WORKFLOW record type (AMW 2409.07429, ExpeL 2308.10144).
        # Step-by-step procedures the operator confirmed working, distinct
        # from a DECISION (why) and a FACT (what): the workflow is the how.
        # Databases that already reached v12 skip the v10 block, so the
        # CHECK rebuild (SQLite cannot ALTER a CHECK) runs here too: the
        # guard checks the live CHECK, not the schema version.
        if _rebuild_records_check(conn):
            _reconcile_fts(conn)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (13)")
        conn.commit()

    if version < 14:
        # v14: PROCEDURAL + REFLECTIVE types (MemBench 2506.21605).
        # PROCEDURAL is a repeatable how-to the operator executed (steps,
        # commands, worked sequence); REFLECTIVE is a distilled lesson
        # about process or judgment (what worked, what to do differently).
        # Same CHECK-rebuild pattern as v13: the guard checks the live
        # CHECK, so a v12 database replays v13 and v14 blocks in order.
        if _rebuild_records_check(conn):
            _reconcile_fts(conn)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (14)")
        conn.commit()

    if version < 15:
        # v15: per-record memory strength + temporal validity + code anchors.
        # decay_tau=0 means "use the global curve"; reinforce(success) grows
        # it (spacing effect, Cepeda 2006 / Duolingo HLR), failure shrinks it.
        # valid_from/until give bi-temporal as_of recall (Graphiti/Mnemosyne);
        # anchor_path/sha let a capture pass mark records stale when the file
        # they describe changes. All additive: existing rows keep behavior.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        for col, ddl in (
            ("decay_tau", "ALTER TABLE records ADD COLUMN decay_tau REAL NOT NULL DEFAULT 0"),
            ("valid_from", "ALTER TABLE records ADD COLUMN valid_from TEXT NOT NULL DEFAULT ''"),
            ("valid_until", "ALTER TABLE records ADD COLUMN valid_until TEXT NOT NULL DEFAULT ''"),
            ("anchor_path", "ALTER TABLE records ADD COLUMN anchor_path TEXT NOT NULL DEFAULT ''"),
            ("anchor_sha", "ALTER TABLE records ADD COLUMN anchor_sha TEXT NOT NULL DEFAULT ''"),
            ("stale", "ALTER TABLE records ADD COLUMN stale INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in cols:
                conn.execute(ddl)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (15)")
        conn.commit()

    if version < 16:
        # v16: write staging (hermes write_approval). Auto-capture candidates
        # below the trust threshold wait here for approve/reject instead of
        # landing in records directly. content_hash pins the candidate at
        # staging time: apply refuses if the payload changed since review.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_writes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rtype TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.35,
                source TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                project_id TEXT NOT NULL DEFAULT '',
                event_id TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT NOT NULL DEFAULT ''
            )""")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (16)")
        conn.commit()

    if version < 17:
        # v17: adjudicated non-conflicts (engram not_conflict pattern). A
        # pair the agent judged compatible never resurfaces in conflicts().
        conn.execute("""
            CREATE TABLE IF NOT EXISTS not_conflict (
                a TEXT NOT NULL,
                b TEXT NOT NULL,
                ts TEXT NOT NULL,
                PRIMARY KEY (a, b)
            )""")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (17)")
        conn.commit()

    if version < 18:
        # v18: consolidated_at closes the consolidation ratchet. Without it
        # the same three retrieval rows paid out CONSOLIDATE_GAIN on every
        # sleep pass, pinning any recalled record at confidence 1.0 within
        # hours and permanently defeating decay.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        if "consolidated_at" not in cols:
            conn.execute(
                "ALTER TABLE records ADD COLUMN consolidated_at TEXT NOT NULL DEFAULT ''")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (18)")
        conn.commit()

    # Every migration path must leave the index consistent with the table.
    _reconcile_fts(conn)


def init_db(conn: sqlite3.Connection, db_path: str = None, auto_repair: bool = True):
    """Create or migrate the schema on an open connection.

    Raises sqlite3.DatabaseError when the database is unusable. The caller's
    connection is never closed and the file is never removed; a corrupt
    database is the operator's decision, made explicitly via repair_from_backup.
    """
    if not quick_check(conn) and _has_new_columns(conn):
        # quick_check flags NULL in a NOT NULL column added by ALTER TABLE,
        # where SQLite cannot backfill the default on existing rows.
        conn.execute("UPDATE records SET importance=0.5 WHERE importance IS NULL")
        conn.execute("UPDATE records SET cue='' WHERE cue IS NULL")
        conn.execute("UPDATE records SET superseded_by='' WHERE superseded_by IS NULL")
        conn.execute("UPDATE records SET decay_exempt_until='' WHERE decay_exempt_until IS NULL")
        conn.commit()
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
                  parent_id: str = "", event_id: str = "",
                  happened_at: str = "") -> str:
    if rtype not in VALID_TYPES:
        raise ValueError(f"invalid type: {rtype}")
    if not content:
        raise ValueError("content must not be empty")

    confidence = min(1.0, max(0.0, float(confidence)))
    project_key = project_id or ""
    session_key = session_id or ""
    # happened_at lets an import or a benchmark place a record at its real
    # time. It defaults to now, so every existing caller is unchanged, but
    # without it a temporal benchmark is inert: every row would look
    # one second old and the recency/decay machinery could never be tested.
    stamp = happened_at or now_iso()
    record_id = record_id_for(rtype, content, source, project_key)

    existing = conn.execute(
        "SELECT id FROM records WHERE type=? AND content=? AND source=? AND project_id=?",
        (rtype, content, source, project_key)
    ).fetchone()

    if existing:
        record_id = existing["id"]
        # event_id merges rather than overwrites: a re-capture of the same
        # lesson in a later event keeps the earlier grouping.
        if not event_id:
            row = conn.execute("SELECT event_id FROM records WHERE id=?",
                               (record_id,)).fetchone()
            event_id = row["event_id"] if row else ""
        conn.execute(
            "UPDATE records SET updated_at=?, evidence=?, verified=?, confidence=?,"
            " tags=?, session_id=?, parent_id=?, event_id=? WHERE id=?",
            (stamp, evidence or "", 1 if verified else 0, confidence,
             tags or "", session_key, parent_id or "", event_id, record_id)
        )
    else:
        conn.execute("""
            INSERT INTO records
                (id,type,content,evidence,verified,confidence,source,tags,
                 created_at,updated_at,session_id,project_id,parent_id,event_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (record_id, rtype, content, evidence or "", 1 if verified else 0, confidence,
              source or "", tags or "", stamp, stamp, session_key, project_key,
              parent_id or "", event_id or ""))

    conn.commit()
    return record_id


def _insert_identity(conn: sqlite3.Connection, record_id: str, rtype: str, norm: str,
                     evidence: str, verified: int, confidence: float, source: str,
                     tags: str, session_id: str, project_id: str, parent_id: str,
                     about_peer: str = "", event_id: str = "",
                     importance: float = 0.5, cue: str = ""):
    stamp = now_iso()
    conn.execute("""
        INSERT INTO records
            (id,type,content,evidence,verified,confidence,source,tags,
             created_at,updated_at,session_id,project_id,parent_id,project_ids,
             about_peer,event_id,importance,cue)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (record_id, rtype, norm, evidence or "", 1 if verified else 0, confidence,
          source or "", tags or "", stamp, stamp, session_id or "", project_id or "",
          parent_id or "", project_id or "", about_peer or "", event_id or "",
          importance, cue))


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
        rows = search(conn, content, limit=limit * 8, rtype=rtype,
                      allow_repair=False, rank=False)
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
    # SQLite caps LIKE pattern length (a 60k-character token raised
    # "LIKE or GLOB pattern too complex" and killed the whole write). The
    # first MAX_LIKE_TOKEN characters are enough to identify the row; a
    # truncated token can only make the prefilter broader, and Jaccard
    # scoring downstream still decides.
    tokens = [t[:MAX_LIKE_TOKEN] for t in tokens]
    sql += " AND (" + " OR ".join("content LIKE ?" for _ in tokens) + ")"
    params.extend(f"%{tok}%" for tok in tokens)
    # Newest-first so SQLite can stop early: a LIKE over content has no
    # index, and scanning the whole table cost 10ms per write at 12k
    # records (another linear term in the ingest curve). An advisory
    # near-duplicate check only needs recent candidates anyway.
    sql += " ORDER BY rowid DESC LIMIT ?"
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


IDF_VETO_DF_RATIO = 0.02   # a token in <=2% of the corpus is discriminating
IDF_VETO_MIN_DF = 2        # never veto on a token that appears once


def _idf_veto(conn: sqlite3.Connection, a: str, b: str) -> bool:
    """Fellegi-Sunter blocking-key veto on a *relative* rarity test.

    The earlier absolute theta=2.0 was not a rarity test: log(1+N/(1+df))>2
    means df < 15.5% of the corpus at large N, is unsatisfiable for N<=6
    (so the shipped tests never exercised it), and for N>=7 fires on any
    token the other side merely lacks -- blocking legitimate paraphrase
    merges. Rarity is now stated directly as a document-frequency ratio
    with a floor, so a token present in one or two records is only treated
    as a blocking key once the corpus is large enough for that to mean
    something.
    """
    total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] or 0
    if total < 8:
        return False
    limit = max(IDF_VETO_MIN_DF, int(total * IDF_VETO_DF_RATIO))
    sa = set(_tokens(a))
    sb = set(_tokens(b))
    for t in sa ^ sb:
        if len(t) > MAX_LIKE_TOKEN:
            continue
        df = conn.execute(
            "SELECT COUNT(*) FROM records WHERE content LIKE ?",
            (f"%{t}%",)).fetchone()[0]
        if 0 < df <= limit:
            return True
    return False


def _upsertable(candidate: dict, norm: str, conn: sqlite3.Connection = None) -> bool:
    """Guards: high score, identical numbers, and no rare-token veto."""
    if float(candidate.get("score") or 0.0) < UPSERT_THRESHOLD:
        return False
    if _numeric_tokens(candidate.get("content") or "") != _numeric_tokens(norm):
        return False
    if conn is not None and _idf_veto(conn, candidate.get("content") or "", norm):
        return False
    return True


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

    Returns action update|create|delete|noop, the id it would touch, the
    reason above, the similar candidates and the human-readable advisory.
    Never writes.
    """
    norm = normalize_content(content)
    own = content_id(norm)
    existing = get_record(conn, own)
    if existing:
        # Mem0-style taxonomy: not every write deserves a write. A restatement
        # of a discredited fact asks for deletion (the fact is wrong and the
        # duplicate adds nothing); a restatement of a fully-confirmed fact
        # asks for nothing at all.
        signals = _record_signals(conn, own)
        if signals and signals[-1].get("signal") == "failure" \
                and not existing["verified"]:
            return {"action": "delete", "id": own, "reason": "identical",
                    "similar": [],
                    "advisory": ("repeats a discredited fact; delete instead "
                                 "of re-storing it")}
        if existing["verified"] and float(existing["confidence"] or 0) >= 0.95:
            return {"action": "noop", "id": own, "reason": "identical",
                    "similar": [],
                    "advisory": "already confirmed; nothing to learn"}
        return {"action": "update", "id": own, "reason": "identical",
                "similar": [], "advisory": ""}

    similar = find_similar(conn, norm, limit=limit, threshold=threshold, rtype=rtype)
    mergeable = [c for c in similar if upsert and _upsertable(c, norm, conn)]
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


def _rif_supersede(conn, record_id: str, rtype: str, norm: str,
                   similar: list) -> list:
    """Penalize records this write supersedes (Anderson et al. 1994).

    Only a CORRECTION does it, and only when the loser is well-trusted:
    a low-confidence loser is already on its way out, and inhibiting it
    buys nothing. The loser keeps its decay curve and can recover through
    reinforcement, which is what makes RIF inhibition rather than
    deletion.
    """
    if rtype != "CORRECTION":
        return []
    # decide() scoped its similarity search to the incoming type, so a
    # CORRECTION never saw the FACT it supersedes. RIF must look across types.
    similar = find_similar(conn, norm, limit=5, threshold=SIMILAR_THRESHOLD)
    penalized = []
    for cand in similar:
        if cand["id"] == record_id:
            continue
        conf = float(cand.get("confidence") or 0.0)
        if conf < RIF_MIN_CONFIDENCE:
            continue
        new_conf = round(max(0.0, conf - RIF_PENALTY), 6)
        conn.execute("UPDATE records SET confidence=? WHERE id=?",
                     (new_conf, cand["id"]))
        conn.execute(
            "INSERT INTO reinforcement (record_id, signal, delta, source, ts)"
            " VALUES (?, 'failure', ?, 'rif', ?)",
            (cand["id"], -RIF_PENALTY, now_iso()))
        penalized.append({"id": cand["id"], "from": conf, "to": new_conf})
    conn.commit()
    return penalized


def _set_superseded(conn, record_id: str, source: str,
                    prev_content: str = None) -> list:
    """Mark near-duplicates as superseded by a rewritten record.

    A correction that replaced content left the old content in a
    near-duplicate somewhere. RIF already penalized them at write time;
    this links the loser to the winner so recall can downrank it and a
    caller can follow the chain. Similarity runs against the PRE-rewrite
    content when the rewrite is what linked this record: the losers are
    near-duplicates of what the record used to say. When the old content
    scores nothing above threshold, the new content is tried too: a
    correction can narrow the claim (prod-01 becomes prod-02) and the
    near-duplicate was only ever close to the narrower form.
    """
    rewritten = conn.execute(
        "SELECT content FROM records WHERE id=?", (record_id,)).fetchone()
    if rewritten is None:
        return []
    similar = find_similar(conn, prev_content, limit=5,
                           threshold=SIMILAR_THRESHOLD) if prev_content else []
    if not similar:
        similar = find_similar(conn, rewritten[0], limit=5,
                               threshold=SIMILAR_THRESHOLD)
    own_numbers = _numeric_tokens(rewritten[0])
    similar = [c for c in similar
               if _numeric_tokens(c["content"]) == own_numbers]
    # Close the loser's validity window at the winner's valid_from, the
    # Graphiti edge-invalidation rule: bi-temporal as_of must not return a
    # fact whose window should already be shut. The old code set only the
    # superseded_by pointer, so as_of queries kept serving the stale fact.
    winner_from = conn.execute(
        "SELECT valid_from, updated_at FROM records WHERE id=?",
        (record_id,)).fetchone()
    close_at = (winner_from[0] if winner_from and winner_from[0] else
                (winner_from[1] if winner_from else ""))
    linked = []
    for cand in similar:
        if cand["id"] == record_id:
            continue
        if close_at:
            conn.execute(
                "UPDATE records SET superseded_by=?,"
                " valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END"
                " WHERE id=? AND superseded_by=''",
                (record_id, close_at, cand["id"]))
        else:
            conn.execute(
                "UPDATE records SET superseded_by=? WHERE id=?"
                " AND superseded_by=''",
                (record_id, cand["id"]))
        linked.append(cand["id"])
    # Chain resolution: rows superseded by the rewritten record may
    # themselves be the target of older markers (A -> B, B -> C). After
    # B is linked to C, A still points at a loser, so every row pointing
    # at B follows to C: the chain resolves to its head in one hop, and
    # no query walks A -> B -> C at read time.
    if linked:
        losers = [rid for rid in linked if not str(rid).startswith("chain:")]
        for loser in losers:
            conn.execute(
                "UPDATE records SET superseded_by=? WHERE superseded_by=?"
                " AND id != ?",
                (record_id, loser, record_id))
        linked.append(f"chain:{losers}->{record_id}" if losers
                      else f"chain:none->{record_id}")
    return linked


def log_retrieval(conn, record_ids: list) -> int:
    """Log one zero-delta retrieval signal per record (Roediger & Karpicke 2006).

    A retrieval is a learning event, not a confirmation: delta 0 moves no
    confidence, but it starts the spacing clock and counts toward
    consolidation.
    """
    n = 0
    for rid in record_ids:
        cur = conn.execute(
            "INSERT INTO reinforcement (record_id, signal, delta, source, ts)"
            " VALUES (?, 'retrieval', 0.0, 'recall', ?)", (rid, now_iso()))
        n += cur.rowcount
    conn.commit()
    return n


def consolidate(conn, now=None) -> list:
    """Reward repeated retrieval with a small confidence gain.

    Roediger & Karpicke 2006: retrieval practice consolidates. A record
    pulled back RETRIEVALS_FOR_CONSOLIDATION times since its last
    consolidation with no success or failure attached is being used
    without contradiction; it earns CONSOLIDATE_GAIN once.

    Two invariants keep this from becoming a ratchet:
    - Retrievals are counted only AFTER the record's last consolidation
      (`consolidated_at`), so the same three rows cannot pay out twice.
    - updated_at is NOT touched. It is the recency signal and one of
      apply_decay's anchors; resetting it on every pass made a frequently
      recalled record immortal (confidence pinned at 1.0 within hours and
      decay forever looking at a fresh timestamp).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=RETRIEVAL_WINDOW_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT r.id, r.confidence, r.consolidated_at,
               (SELECT count(*) FROM reinforcement x
                 WHERE x.record_id = r.id AND x.signal = 'retrieval'
                   AND x.ts >= ? AND x.ts > r.consolidated_at) AS retrievals,
               (SELECT count(*) FROM reinforcement x
                 WHERE x.record_id = r.id AND x.signal = 'success'
                   AND x.ts >= ? AND x.ts > r.consolidated_at) AS successes,
               (SELECT count(*) FROM reinforcement x
                 WHERE x.record_id = r.id AND x.signal = 'failure'
                   AND x.ts >= ? AND x.ts > r.consolidated_at) AS failures
        FROM records r
        WHERE r.verified = 0 AND r.superseded_by = ''
        """, (cutoff, cutoff, cutoff)).fetchall()
    consolidated = []
    for row in rows:
        if row["retrievals"] < RETRIEVALS_FOR_CONSOLIDATION:
            continue
        if row["successes"] or row["failures"]:
            continue
        conf = min(1.0, float(row["confidence"] or 0.0) + CONSOLIDATE_GAIN)
        conn.execute("UPDATE records SET confidence=?, consolidated_at=? WHERE id=?",
                     (conf, now.isoformat(), row["id"]))
        consolidated.append({"id": row["id"], "from": row["confidence"],
                             "to": conf})
    conn.commit()
    return consolidated


def sleep_pass(conn, now=None, budget: int = SLEEP_EVICT_BUDGET,
               prune_similarity: float = SLEEP_PRUNE_SIMILARITY,
               apply: bool = True) -> dict:
    """One offline consolidation cycle over the whole store (wake/sleep split).

    Lin et al. 2025: memory systems improve when maintenance happens between
    queries, not inside them. This pass is exactly that: decay ages
    confidence, consolidation rewards retrieved records, near-duplicate rows
    collapse into their keepers, and projects over budget evict their
    Unscoped records and verified/RULE/CORRECTION rows are never deleted.

    Deletion is conservative (rate-distortion): the only deletes this pass
    performs are prune merges (recoverable, the keeper holds the fact). A
    project over budget does NOT lose its lowest-value rows: those are the
    last holders of their content and a delete is irreversible, so they stay
    and are reported in "kept" (reason "over budget, irreplaceable") for the
    operator to decide. The prune already collapsed every 0.80+ overlap, so
    the evict loop can only ever see rows below that band.

    apply=False reports the same plan without writing: deleting memory is a
    decision, not a side effect.
    """
    now = now or datetime.now(timezone.utc)
    decayed = apply_decay(conn, now=now)
    consolidated = consolidate(conn, now=now)
    report = {"decayed": decayed, "consolidated": consolidated,
              "merged": [], "evicted": [], "kept": [], "apply": bool(apply)}

    # Prune: near-duplicate unverified rows collapse into the older keeper.
    rows = [dict(r) for r in conn.execute(
        "SELECT id, content, created_at FROM records WHERE verified=0"
        " ORDER BY created_at")]
    for i, row in enumerate(rows):
        already_merged = {r["merged"] for r in report["merged"]}
        keepers = {r["keeper"] for r in report["merged"]}
        if row["id"] not in already_merged | keepers and \
                not _record_gone(conn, row["id"]):
            for other in rows[i + 1:]:
                if _record_gone(conn, other["id"]):
                    continue
                if similarity(row["content"], other["content"]) >= prune_similarity:
                    if apply:
                        conn.execute(
                            "INSERT OR REPLACE INTO forgotten_graveyard"
                            " (id, type, content, reason, ts) VALUES (?,?,?,?,?)",
                            (other["id"], "DUPLICATE",
                             (other["content"] or "")[:200],
                             f"sleep: near-duplicate of {row['id']}",
                             now.isoformat()))
                        conn.execute("DELETE FROM reinforcement WHERE record_id=?",
                                     (other["id"],))
                        conn.execute("DELETE FROM records WHERE id=?", (other["id"],))
                    report["merged"].append({"keeper": row["id"],
                                             "merged": other["id"]})

    # Evict: per project, rows beyond budget leave lowest-value first.
    counts = conn.execute(
        "SELECT project_id, count(*) AS n FROM records"
        " WHERE verified=0 GROUP BY project_id").fetchall()
    for proj in counts:
        pid = proj["project_id"] or ""
        n = proj["n"]
        if n <= budget or pid == "":
            continue
        over = n - budget
        rows = [dict(r) for r in conn.execute(
            "SELECT id, content, confidence, updated_at FROM records"
            " WHERE project_id=? AND verified=0", (pid,))]
        for row in rows:
            age = 0.0
            ts = _parse_ts(row["updated_at"])
            if ts:
                age = max(0.0, (now - ts).total_seconds() / 86400.0)
            row["value"] = float(row["confidence"] or 0.0) * (
                (1.0 + age / DECAY_POWER_TAU_DAYS) ** -DECAY_POWER_BETA)
        rows.sort(key=lambda r: r["value"])
        for row in rows[:over]:
            # Report-only: the prune already collapsed every 0.80+ overlap,
            # so these are the last holders of their content. Deleting them
            # is irreversible, and the operator, not the sleep pass, owns
            # that decision.
            report["kept"].append({"id": row["id"], "project": pid,
                                   "reason": "over budget, irreplaceable",
                                   "value": round(row["value"], 6)})
    if apply and (report["merged"] or report["evicted"]):
        conn.commit()
    # Sleep is the natural backup moment: offline, consistent, and the
    # restore story is meaningless without a snapshot that actually runs.
    if apply:
        report["backup"] = backup_db(db_path_of(conn))
    return report


def _record_gone(conn, record_id: str) -> bool:
    return conn.execute("SELECT 1 FROM records WHERE id=?",
                        (record_id,)).fetchone() is None


def remember(conn: sqlite3.Connection, rtype: str, content: str, evidence: str = "",
             verified: int = 0, confidence: float = 0.5, source: str = "",
             tags: str = "", session_id: str = "", project_id: str = "",
             parent_id: str = "", upsert: bool = False, limit: int = 5,
             about_peer: str = "", event_id: str = "",
             importance: float = 0.5, cue: str = "",
             valid_from: str = "", valid_until: str = "") -> dict:
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
            " project_ids=?, updated_at=?, about_peer=?, event_id=?"
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
             about_peer or existing["about_peer"],
             event_id or existing["event_id"],
             record_id)
        )
        applied = "updated"
    else:
        prior = _schema_prior(confidence, decision["similar"])
        _insert_identity(conn, record_id, rtype, norm, evidence, verified, prior,
                         source, tags, session_id, project_id, parent_id,
                         about_peer, event_id,
                         importance=min(1.0, max(0.0, float(importance))),
                         cue=normalize_cue(cue))
        applied = "inserted"

    conn.commit()
    if valid_from or valid_until:
        conn.execute("UPDATE records SET valid_from=?, valid_until=? WHERE id=?",
                     (valid_from, valid_until, record_id))
        conn.commit()
    rif_applied = _rif_supersede(conn, record_id, rtype, norm,
                                 decision["similar"])
    advisory = decision["advisory"]
    plan_note = _future_tense_note(content, valid_from)
    if plan_note:
        advisory = (advisory + " " + plan_note).strip() if advisory else plan_note
    return {"id": record_id, "type": rtype, "shape": norm, "applied": applied,
            "rif": rif_applied,
            "action": decision["action"], "reason": decision["reason"],
            "similar": decision["similar"], "advisory": advisory}


# Future-tense markers (graphiti temporal_edge_utils.py pattern): a plan is
# not a present fact. When wording says "will/akan" and the caller pinned a
# validity window, remind them the window should start when the plan
# executes, not when it was stated. Advisory only - never blocks a write.
_FUTURE_RE = re.compile(
    r"\b(will|shall|going to|plans? to|planned|next (sprint|week|month|release)"
    r"|akan|nanti|rencana|mau di)\b", re.IGNORECASE)


def _future_tense_note(content: str, valid_from: str) -> str:
    if valid_from and _FUTURE_RE.search(content or ""):
        return ("plan wording detected: valid_from should mark when the plan "
                "takes effect, not when it was stated")
    return ""


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

_VOCAB_CACHE: dict = {}
_VOCAB_CACHE_LOCK = threading.Lock()


def _vocab(conn: sqlite3.Connection) -> list:
    """Distinct stemmed terms in the index, for typo repair.

    fts5vocab is a read-only mirror of the index, so it is always in sync
    and never needs maintenance. Missing table means an uninitialised or
    in-memory database; an empty vocabulary makes every repair a no-op.

    Loading the full term list was the top cost of a rerank (0.35s of 1.4s
    over five queries) because it ran twice per query. The list is cached
    per (database file, sqlite data_version): any write by another
    connection bumps data_version and invalidates the entry, so the cache
    cannot go stale silently.
    """
    try:
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        # data_version only bumps on commits from OTHER connections, so the
        # same-connection writer needs the record count too: any insert or
        # delete changes it, and an FTS term set cannot change without one.
        version = (conn.execute("PRAGMA data_version").fetchone()[0],
                   conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])
    except sqlite3.OperationalError:
        db_file, version = "", (-1, -1)
    key = (db_file, version)
    with _VOCAB_CACHE_LOCK:
        cached = _VOCAB_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        vocab = [r[0] for r in conn.execute(
            "SELECT term FROM records_fts_vocab ORDER BY term")]
    except sqlite3.OperationalError:
        vocab = []
    with _VOCAB_CACHE_LOCK:
        _VOCAB_CACHE[key] = vocab
        if len(_VOCAB_CACHE) > 8:
            _VOCAB_CACHE.pop(next(iter(_VOCAB_CACHE)))
    return vocab


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
        if len(w) < MIN_REPAIR_LEN or _WORD_TO_GROUP.get(w):
            # Short words clear the fuzzy cutoff too easily; known
            # synonyms are real words, not typos - the synonym tier
            # translates them, difflib must not mangle them first.
            fixed.append(w)
            continue
        close = difflib.get_close_matches(w, vocab, n=1, cutoff=FUZZY_CUTOFF)
        fixed.append(close[0] if close else w)
    return fixed


def _search_after_repairs(conn: sqlite3.Connection, query: str,
                          allow_repair: bool = True, rank: bool = True,
                          **kwargs) -> list:
    """Search exact first; on an empty result, retry with repaired words.

    allow_repair=False skips the fuzzy stage. The write path
    (decide -> find_similar -> _candidates) uses it: a near-duplicate
    lookup does not need typo correction, and difflib over the whole
    vocabulary (9.6k terms) cost ~46ms per write at 1.5k records -- the
    dominant term in a superlinear ingest curve (40ms at 500 rows to 81ms
    at 4k).
    """
    rows = _search_exact(conn, query, rank=rank, **kwargs)
    if rows or not allow_repair:
        return rows
    vocab = _vocab(conn)
    words = _repair_typos(conn, _tokens(query), vocab)
    if words != _tokens(query):
        rows = _search_exact(conn, " ".join(words), rank=rank, **kwargs)
    return rows


def _stem(word: str) -> str:
    """Porter-ish suffix strip matching the FTS tokenizer's common cases."""
    # porter step 1b: *ied under a consonant just drops the d, so
    # "verified" rejoins "verify" before step 1c
    if word.endswith("ied") and len(word) >= 5:
        word = word[:-1]
    for suffix in ("ization", "ational", "iveness", "fulness", "ousness",
                   "ation", "ement", "ances", "ences", "ingly", "edly",
                   "ings", "ies", "ally", "es", "ed", "ing", "er",
                   "est", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    # porter step 1c: trailing y after a consonant becomes i, so
    # "verify" and "verified" stem to "verifi"
    if word.endswith("y") and len(word) >= 3 and word[-2] not in "aeiou":
        word = word[:-1] + "i"
    elif word.endswith("ie") and len(word) >= 4:
        word = word[:-2] + "i"
    return word


def _or_query(words: list) -> str:
    """Build an OR match: any word hitting is enough to surface a candidate."""
    return " OR ".join('"' + w + '"*' for w in words)


# Concept groups for the synonym fallback tier (pattern adopted from
# Mnemosyne core/synonyms.py, code-audited 2026-11-21). Curated, small,
# lowercase; a word resolves to at most one group. Expansion runs only
# when every earlier tier came back empty, so a working query's ranking
# is never widened.
SYNONYM_GROUPS = {
    "database": ["db", "datastore", "sqlite", "postgres"],
    "password": ["pass", "pwd", "passwd", "credential", "secret"],
    "config": ["configuration", "settings", "cfg"],
    "error": ["bug", "fault", "failure", "crash", "exception", "traceback"],
    "fix": ["repair", "resolve", "patch", "correct"],
    "deploy": ["deployment", "release", "rollout"],
    "server": ["host", "machine", "vps", "instance"],
    "api": ["endpoint", "interface"],
    "token": ["apikey", "bearer"],
    "user": ["account", "profile", "identity"],
    "model": ["llm", "provider"],
    "latency": ["slow", "lag", "timeout", "deadline"],
    "memory": ["recall", "remember", "retention"],
    "search": ["find", "lookup", "query", "retrieve"],
    "file": ["document", "doc", "path"],
    "delete": ["remove", "drop", "erase", "purge"],
    "create": ["add", "insert", "new"],
    "update": ["edit", "modify", "change", "upgrade"],
    "test": ["pytest", "spec", "assert"],
    "install": ["setup", "pip", "npm", "dependency"],
    "backup": ["snapshot", "dump", "restore"],
    "rotate": ["rotation", "refresh", "renew"],
}
_WORD_TO_GROUP = {}
for _canonical, _group in SYNONYM_GROUPS.items():
    for _w in [_canonical] + _group:
        _WORD_TO_GROUP.setdefault(_w, _canonical)


def _synonym_group(word: str):
    canonical = _WORD_TO_GROUP.get(word)
    if canonical is None:
        return None
    return [canonical] + SYNONYM_GROUPS[canonical]


def _synonym_query(query: str) -> str:
    """OR-of-groups MATCH for the fallback tier; '' when nothing expands.

    Each group becomes ("a" OR "b" ...) and groups AND together, so a
    two-word query still needs both concepts present, just spelled either
    way. Words without a group stay literal. Only fires on an otherwise
    empty result, so precision loss cannot touch a working query.
    """
    words = _tokens(query)
    if not words:
        return ""
    parts, expanded = [], False
    for w in words:
        group = _synonym_group(w)
        if group and len(group) > 1:
            alts = sorted(set(group) | {w})
            parts.append("(" + " OR ".join('"%s"' % a for a in alts) + ")")
            expanded = True
        else:
            parts.append('"%s"*' % w)
    if not expanded:
        return ""
    return ">>" + " AND ".join(parts)


def _concept_overlap(query: str, row: dict) -> int:
    """Query words whose concept group the record satisfies (any spelling).

    The AND-of-OR-groups MATCH already guarantees every concept is present;
    this counts them per record so the lexical fraction reflects concepts
    covered, not raw string overlap with words the operator never stored.
    """
    row_tokens = {_stem(w) for w in _tokens(" ".join(
        (row.get("content") or "", row.get("evidence") or "", row.get("tags") or "")))}
    n = 0
    for w in _tokens(query):
        group = _synonym_group(w) or [w]
        if any(_stem(g) in row_tokens for g in group):
            n += 1
    return max(1, n)


def _query_known(conn: sqlite3.Connection, query: str) -> bool:
    """True when at least one query word (or a prefix of it) is indexed.

    graphify vocabulary gate, stdlib: unknown vocabulary means the query is
    outside what this store can answer, so the honest reply is abstention,
    not a partial-token guess. Known synonyms count as known words.

    Probed with three indexed lookups instead of materialising the whole
    vocabulary: _vocab() cost ~17ms per call at 4k records and the write
    path calls this once per write, which made ingest superlinear. An
    empty index still returns True so a fresh store does not abstain on
    everything.
    """
    # COUNT(*) over fts5vocab walks every term (17ms at 12k records); the
    # records count is O(1) and answers the same question (is the index
    # empty?). _query_known runs once per write, so the 17ms was a real
    # term in the ingest curve.
    total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    if not total:
        return True
    for w in _tokens(query):
        s = _stem(w)
        row = conn.execute(
            "SELECT term FROM records_fts_vocab WHERE term >= ?"
            " ORDER BY term LIMIT 1", (s,)).fetchone()
        if row and (row[0] == s or row[0].startswith(s)):
            return True
        if _WORD_TO_GROUP.get(w):
            return True
    return False


def _search_relaxed(conn: sqlite3.Connection, query: str,
                    allow_repair: bool = True, rank: bool = True, **kwargs) -> list:
    """When no record contains every word, rank by best single-word match.

    A four-word natural-language query ("ganti model pindah sesi") usually
    has no record containing all four, and an AND-only index answers zero
    even though several records cover the intent well. bm25 already ranks
    an OR pool by how strongly each record matches its best term, so the
    top rows are the ones sharing the most/strongest words with the query,
    not random one-word hits. Runs only after the exact AND and the
    typo-repaired retry both return nothing.

    An INTENT whose cue carries a query term is pooled even when its body
    matches nothing: the cue is the environment signal that should pull the
    intent forward (Rummel & Kvavilashvili 2023). Cue hits ride at the end
    of the pool; the cue bonus in recall() does the ranking.
    """
    rows = _search_after_repairs(conn, query, allow_repair=allow_repair,
                                 **kwargs)
    if not rows:
        # Concept translation beats word soup: a record covering BOTH
        # concepts under different spellings is a better answer than one
        # sharing a single literal word, so the synonym tier runs before
        # the OR relaxation.
        syn_expr = _synonym_query(query)
        if syn_expr:
            rows = _search_exact(conn, syn_expr, rank=rank, **kwargs)
            for r in rows:
                # Provenance: matched via concept translation, not the
                # operator's literal words. recall() surfaces the flag.
                r["synonym_match"] = True
                r["matched_terms"] = _concept_overlap(query, r)
                r["query_terms"] = max(1, len(_tokens(query)))
    if not rows and not _query_known(conn, query):
        # Vocabulary gate (graphify _query_uses_known_words): a query whose
        # words exist nowhere in the index cannot be answered from one
        # incidental shared token; abstain instead of guessing. Cue hits
        # are exempt: an INTENT's cue is deliberately outside the FTS
        # index, so a cue-only match is still a real answer.
        cue_rows = _cue_pool(conn, query, **kwargs)
        return cue_rows
    if not rows:
        words = _tokens(query)
        if len(words) >= 2:
            or_expr = _or_query(words)
            if or_expr:
                rows = _search_exact(conn, ">" + or_expr, rank=rank, **kwargs)
    cue_rows = _cue_pool(conn, query, **kwargs)
    if cue_rows:
        seen = {r["id"] for r in rows}
        rows = rows + [r for r in cue_rows if r["id"] not in seen]
    return rows


def _cue_pool(conn: sqlite3.Connection, query: str, limit: int = 20, rtype: str = None,
              project_id: str = None, session_id: str = None,
              verified_only: bool = False,
              min_confidence: float = 0.0, pool: int = None,
              about_peer: str = None, as_of: str = None) -> list:
    """INTENT records whose cue shares a token with the query.

    Content search cannot see the cue (it is not in the FTS index), so this
    is a plain scan over the cue column. Kept small by the intent table's
    size; returns rows shaped like _search_exact's so recall()'s reranker
    can treat them uniformly.
    """
    qtokens = _tokens(query)
    if not qtokens:
        return []
    sql = """
        SELECT r.id, r.type, r.content, r.evidence, r.verified, r.confidence,
               r.source, r.tags, r.created_at, r.updated_at, r.session_id,
               r.project_id, r.parent_id, r.about_peer, r.superseded_by,
               r.importance, r.cue, r.decay_tau, r.stale, r.valid_from,
               r.valid_until, 0.0 AS rank
        FROM records r
        WHERE r.type = 'INTENT'
    """
    params = []
    if rtype:
        sql += " AND r.type = ?"
        params.append(rtype)
    if project_id:
        sql += " AND r.project_id = ?"
        params.append(project_id)
    if session_id:
        sql += " AND r.session_id = ?"
        params.append(session_id)
    if about_peer:
        sql += " AND r.about_peer = ?"
        params.append(about_peer)
    if verified_only:
        sql += " AND r.verified = 1"
    if min_confidence > 0:
        sql += " AND r.confidence >= ?"
        params.append(min_confidence)
    if as_of:
        sql += (" AND (r.valid_from='' OR r.valid_from<=?)"
                " AND (r.valid_until='' OR r.valid_until>?)")
        params.extend([as_of, as_of])
    rows = [dict(row) for row in conn.execute(sql, params)]
    picked = []
    for row in rows:
        cue_tokens = set(_tokens(row.get("cue") or ""))
        if cue_tokens & set(qtokens):
            row["matched_terms"] = 0
            row["query_terms"] = len(qtokens)
            picked.append(row)
    return picked[:pool if pool is not None else limit]


def _search_exact(conn: sqlite3.Connection, query: str, limit: int = 20, rtype: str = None,
                  project_id: str = None, session_id: str = None,
                  verified_only: bool = False,
                  min_confidence: float = 0.0, pool: int = None,
                  about_peer: str = None, as_of: str = None,
                  rank: bool = True):
    raw = query.startswith('>')
    match = query.lstrip('>') if raw else _fts_query(query)
    if not match:
        return []
    # bm25() in the SELECT list is evaluated per matching row: on a broad OR
    # query that matches the whole corpus it costs ~70ms because FTS5 scores
    # every candidate before LIMIT applies. The near-duplicate prefilter
    # only needs rows, so rank=False omits the scoring entirely.
    # rank=False drops the column entirely: a literal "0.0 AS rank" kept the
    # same three-column projection shape and measured *slower* than bm25,
    # because SQLite no longer used the bare MATCH plan.
    rank_expr = (""", bm25(records_fts, 0.0, 5.0, 1.0, 0.5) / CASE r.type
                   WHEN 'RULE' THEN 1.4 WHEN 'CORRECTION' THEN 1.4
                   WHEN 'DECISION' THEN 1.2 WHEN 'WORKFLOW' THEN 1.2
                   WHEN 'PROCEDURAL' THEN 1.2 WHEN 'INTENT' THEN 1.0
                   WHEN 'FACT' THEN 1.0 WHEN 'REFLECTIVE' THEN 0.9
                   WHEN 'INFERENCE' THEN 0.8 WHEN 'ASSUMPTION' THEN 0.7
                   ELSE 1.0 END AS rank""" if rank else "")
    sql = """
        SELECT r.id, r.type, r.content, r.evidence, r.verified, r.confidence, r.source, r.tags,
               r.created_at, r.updated_at, r.session_id, r.project_id, r.parent_id,
               r.about_peer, r.superseded_by, r.importance, r.cue, r.decay_tau,
               r.stale, r.valid_from, r.valid_until""" + rank_expr + """
        FROM records_fts
        JOIN records r ON r.id = records_fts.id
        WHERE records_fts MATCH ?
    """
    params = [match]
    if rtype:
        sql += " AND r.type = ?"
        params.append(rtype)
    if project_id:
        sql += " AND r.project_id = ?"
        params.append(project_id)
    if session_id:
        sql += " AND r.session_id = ?"
        params.append(session_id)
    if about_peer:
        sql += " AND r.about_peer = ?"
        params.append(about_peer)
    if verified_only:
        sql += " AND r.verified = 1"
    if min_confidence > 0:
        sql += " AND r.confidence >= ?"
        params.append(min_confidence)
    if as_of:
        sql += (" AND (r.valid_from='' OR r.valid_from<=?)"
                " AND (r.valid_until='' OR r.valid_until>?)")
        params.extend([as_of, as_of])
    # Ranking every candidate with bm25 costs ~12ms when a broad OR query
    # matches the whole corpus (12k rows); the near-duplicate prefilter only
    # needs *candidates*, so it passes rank=False and pays ~0.1ms instead.
    if rank:
        sql += (" ORDER BY rank, r.verified DESC, r.confidence DESC,"
                " r.updated_at DESC LIMIT ?")
    else:
        sql += " LIMIT ?"
    params.append(pool if pool is not None else limit)
    rows = [dict(row) for row in conn.execute(sql, params)]
    if not rows:
        return []
    if not rank:
        for row in rows:
            row.setdefault("rank", 0.0)
    # bm25 accumulates per matched term, so the lexical ceiling needs each
    # record's matched-term count: the query terms the record actually
    # matches. AND queries matched every term by construction; OR queries
    # may match a subset. The overlap runs over the same three fields the
    # FTS index covers (content, evidence, tags), and stems both sides
    # with the same suffix rules as the porter tokenizer so "verified"
    # matches "verify". ponytail: suffix-stemming is an approximation of
    # the porter algorithm; wrong stems are possible on irregular words,
    # ceiling: swap for the FTS index itself (one MATCH per term) when
    # lexical precision matters more than latency.
    query_tokens = set(_tokens(query if not raw else
                               " ".join(re.findall(r'"([^"]+)"', match))))
    for r in rows:
        row_text = " ".join((r.get("content") or "", r.get("evidence") or "",
                             r.get("tags") or ""))
        row_tokens = {_stem(w) for w in _tokens(row_text)}
        overlap = len({_stem(w) for w in query_tokens} & row_tokens)
        r["matched_terms"] = max(1, overlap)
        r["query_terms"] = max(1, len(query_tokens))
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
           project_id: str = None, session_id: str = None,
           verified_only: bool = False,
           min_confidence: float = 0.0, pool: int = None,
           about_peer: str = None, as_of: str = None,
           allow_repair: bool = True, rank: bool = True):
    """Exact AND first, then typo-repaired, then best single-word match.

    as_of (ISO timestamp) answers 'what was true then': records whose
    validity window excludes that moment are filtered out (Graphiti /
    Mnemosyne bi-temporal pattern). Empty windows mean always-valid.
    """
    return _search_relaxed(conn, query, limit=limit, rtype=rtype,
                           project_id=project_id, session_id=session_id,
                           allow_repair=allow_repair, rank=rank,
                           verified_only=verified_only,
                           min_confidence=min_confidence, pool=pool,
                           about_peer=about_peer, as_of=as_of)

BM25_CEILING = 10.0


def _absolute_lexical(candidates: list) -> dict:
    """Lexical strength on a fixed [0, 1] scale, keyed by record id.

    bm25 saturates on long OR pools: a record matching 65 of 119 terms
    and one matching 14 can carry similar per-term scores, so bm25 rank
    alone cannot separate a join answer from an incidental hit. The
    primary signal is the matched-term fraction: query terms the record
    actually contains, over the query's term count. bm25 breaks ties
    within the same count, scaled by BM25_CEILING and normalised against
    the best rank in that count's group so it stays [0, 1]. Tiny corpora
    produce tiny bm25 magnitudes, so everything clamps near 1.0 there;
    that is the honest reading (all hits are near-perfect matches) and
    the spread reappears as the corpus grows.
    """
    qtokens = _pool_query_terms(candidates)
    groups = {}
    for c in candidates:
        groups.setdefault(c["matched_terms"] or 0, []).append(c)
    out = {}
    for n, group in groups.items():
        frac = n / qtokens if qtokens else 1.0
        # Within a matched-count bucket bm25 owns the full bucket step
        # (1/nterms): records matching the same term count order by
        # rank, and a stronger rank can beat a heat difference. The
        # spread stays inside the step so the fraction still dominates
        # across buckets, and the whole thing is normalised back to
        # [0, 1] so the documented scale holds.
        step = 1.0 / qtokens
        for c in group:
            tie = min(1.0, max(0.0, -c["rank"] / max(1.0, BM25_CEILING * n)))
            out[c["id"]] = round((frac + tie * step) / (1.0 + step), 6)
    return out


def _pool_query_terms(candidates: list) -> int:
    """Query term count shared by the pool (all rows come from one query)."""
    n = candidates[0].get("query_terms") or 0
    return max(1, n)

def _ppr_expand(conn: sqlite3.Connection, seeds: list, all_rows: list,
                alpha: float = 0.15, iterations: int = 20) -> dict:
    """Personalized PageRank over the record association graph (HippoRAG,
    Gutierrez et al. 2024): seeds are the lexical hits, edges are shared
    rare terms + parent links. Power iteration spreads seed mass to
    associative neighbors that lexical search missed - the multi-hop join
    recall weakness, attacked without embeddings. Returns {id: ppr_mass}.
    """
    if not seeds or not all_rows:
        return {}
    ids = [r["id"] for r in all_rows]
    idx = {rid: i for i, rid in enumerate(ids)}
    n = len(ids)
    postings: dict = {}
    for i, r in enumerate(all_rows):
        for w in set(_tokens(r.get("content") or "")):
            postings.setdefault(w, []).append(i)
    adj: list = [dict() for _ in range(n)]
    for w, members in postings.items():
        if len(members) > 40:  # hub terms link everything; no signal
            continue
        weight = 1.0 / len(members)
        for i in members:
            for j in members:
                if i != j:
                    adj[i][j] = adj[i].get(j, 0.0) + weight
    # Hub penalty (graphify _path_hub_penalty, stdlib): a node that links to
    # a large share of the pool is a hub, and mass through it is association
    # by accident of degree, not meaning. Discount edges into such nodes.
    degree = [len(a) for a in adj]
    hub_cut = max(4, n // 8)
    for i in range(n):
        if degree[i] <= hub_cut:
            continue
        for j in list(adj[i]):
            adj[i][j] *= 0.5
    for i, r in enumerate(all_rows):
        other = r.get("parent_id")
        if other and other in idx:
            j = idx[other]
            adj[i][j] = adj[i].get(j, 0.0) + 1.0
            adj[j][i] = adj[j].get(i, 0.0) + 1.0
    for i in range(n):
        total = sum(adj[i].values())
        if total > 0:
            adj[i] = {j: w / total for j, w in adj[i].items()}
    seed_set = {idx[s["id"]] for s in seeds if s["id"] in idx}
    if not seed_set:
        return {}
    teleport = {i: 1.0 / len(seed_set) for i in seed_set}
    rank = dict(teleport)
    for _ in range(iterations):
        nxt: dict = {}
        for i, mass in rank.items():
            for j, w in adj[i].items():
                nxt[j] = nxt.get(j, 0.0) + (1.0 - alpha) * mass * w
        for i, t in teleport.items():
            nxt[i] = nxt.get(i, 0.0) + alpha * t
        rank = nxt
    # Dangling mass (nodes with no edges) redistributes uniformly so every
    # row has a score; isolated records get the floor, not a KeyError.
    floor = alpha / max(1, n) * 0.1
    return {ids[i]: rank.get(i, floor) for i in range(n)}


PPR_WEIGHT = 0.10


def _ppr_bonus(conn: sqlite3.Connection, seeds: list, all_rows: list) -> dict:
    """Associative bonus per record, bounded by link strength (graphify
    weakest-hop): mass is scaled by the rarest shared term with a seed, so
    a hit joined only through a hub word earns little even when PPR mass is
    high. Returns {id: bonus in [0, PPR_WEIGHT]}.
    """
    ppr = _ppr_expand(conn, seeds, all_rows)
    if not ppr:
        return {}
    ppr_max = max(ppr.values())
    if ppr_max <= 0:
        return {}
    postings: dict = {}
    for i, r in enumerate(all_rows):
        for w in set(_tokens(r.get("content") or "")):
            postings.setdefault(w, []).append(r["id"])
    seed_terms = set()
    for s in seeds:
        seed_terms |= set(_tokens(s.get("content") or ""))
    n = max(1, len(all_rows))
    out = {}
    for r in all_rows:
        terms = set(_tokens(r.get("content") or "")) & seed_terms
        if not terms:
            continue
        # Rarest shared term decides the link strength: 1 member = 1.0,
        # a 40-member hub term = 0.025.
        strength = max(1.0 / len(postings[t]) for t in terms if t in postings)
        out[r["id"]] = PPR_WEIGHT * (ppr.get(r["id"], 0.0) / ppr_max) * min(1.0, strength * 4)
    return out


MMR_LAMBDA = 0.7


def _mmr_select(scored: list, limit: int) -> list:
    """Maximal Marginal Relevance over term overlap (graphiti pattern,
    stdlib): pick the best hit, then prefer candidates that add new terms
    over ones restating what is already selected. Relevance still leads
    (lambda 0.85); only near-duplicates get pushed down, so a window of
    k hits carries k distinct lessons instead of k spellings of one.
    """
    if len(scored) <= 1 or limit <= 1:
        return scored[:limit]
    pool = scored[:max(limit * 2, limit)]
    tokens = [set(_tokens(item[0].get("content") or "")) for item in pool]
    chosen: list = []
    chosen_tokens: set = set()
    remaining = list(range(len(pool)))
    while remaining and len(chosen) < limit:
        best_i, best_v = None, None
        for i in remaining:
            rel = pool[i][5]
            if chosen_tokens and tokens[i]:
                redundancy = len(tokens[i] & chosen_tokens) / len(tokens[i])
            else:
                redundancy = 0.0
            v = MMR_LAMBDA * rel - (1.0 - MMR_LAMBDA) * redundancy
            if best_v is None or v > best_v:
                best_i, best_v = i, v
        chosen.append(pool[best_i])
        chosen_tokens |= tokens[best_i]
        remaining.remove(best_i)
    return chosen


def recall(conn: sqlite3.Connection, query: str, limit: int = 8, full: bool = False,
           pool: int = None, rtype: str = None, project_id: str = None,
           session_id: str = None, now=None,
           verified_only: bool = False, min_confidence: float = 0.0,
           about_peer: str = None, as_of: str = None) -> list:
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
                        session_id=session_id,
                        verified_only=verified_only, min_confidence=min_confidence,
                        about_peer=about_peer, as_of=as_of)
    if not candidates:
        return []
    lexicals = _absolute_lexical(candidates)
    # Associative expansion (HippoRAG): spread seed mass over the term graph
    # so a record that shares rare terms with several hits ranks above one
    # that matched a single common word. Bounded bonus, lexical still leads.
    ppr = _ppr_bonus(conn, candidates[:limit], candidates)

    # Cue triggering (Rummel & Kvavilashvili 2023): a query that carries an
    # INTENT's cue is the cue's appearance in the environment, so the intent
    # is pulled forward. The bonus scales with importance, and fades the
    # longer the intent has waited unfulfilled (Rummel 2023: intentions
    # lose ~.12 force per delay unit).
    qtokens = set(_tokens(query))
    cue_bonus = {}
    for cand in candidates:
        if cand.get("type") != "INTENT":
            continue
        cue = cand.get("cue") or ""
        if not cue or not (qtokens & set(_tokens(cue))):
            continue
        age_days = 0.0
        ts = _parse_ts(cand.get("updated_at"))
        if ts:
            ref = now or datetime.now(timezone.utc)
            age_days = max(0.0, (ref - ts).total_seconds() / 86400.0)
        fade = 1.0 / (1.0 + age_days / INTENT_DECAY_TAU_DAYS)
        cue_bonus[cand["id"]] = INTENT_CUE_BONUS * fade * float(
            cand.get("importance") or 0.5)

    chain_heads = {c["superseded_by"] for c in candidates
                   if c.get("superseded_by")}
    scored = []
    signal_map = _record_signals_bulk(conn, [c["id"] for c in candidates])
    for cand in candidates:
        signals = signal_map.get(cand["id"], [])
        parts = heat_parts(cand, signals, now=now)
        heat = parts["heat"]
        lexical = lexicals[cand["id"]]
        prior = TYPE_PRIOR.get(cand.get("type"), 0.0)
        base = (RECALL_WEIGHTS["lexical"] * lexical
                + RECALL_WEIGHTS["heat"] * heat
                + RECALL_WEIGHTS["type_prior"] * prior)
        superseded = bool(cand.get("superseded_by"))
        penalty = SUPERSEDED_PENALTY if superseded else 0.0
        if (PROVENANCE_PENALTY_ENABLED and not cand.get("verified")
                and str(cand.get("source") or "").strip().casefold()
                in LOW_TRUST_SOURCES):
            penalty += PROVENANCE_PENALTY
        boost = SUPERSEDED_BOOST if cand["id"] in chain_heads else 0.0
        bonus = cue_bonus.get(cand["id"], 0.0)
        assoc = ppr.get(cand["id"], 0.0)
        score = round(max(0.0, base + bonus + boost - penalty + assoc), 6)
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
    top = _mmr_select(scored, limit)
    abstained = bool(top) and top[0][5] < ABSTAIN_SCORE

    out = []
    if full:
        for c, parts, heat, lexical, prior, score in top:
            item = {
                "id": c["id"],
                "type": c["type"],
                "confidence": c["confidence"],
                "verified": c["verified"],
                "score": score,
                "heat": heat,
                "abstained": abstained,
                "content": c.get("content", ""),
                "evidence": c.get("evidence", ""),
                "lexical": lexical,
                "matched_terms": c.get("matched_terms"),
                "type_prior": prior,
                "age_days": parts["age_days"],
                "successes": parts["successes"],
                "about_peer": c.get("about_peer", ""),
                "session_id": c.get("session_id", ""),
                "superseded_by": c.get("superseded_by", ""),
                "importance": c.get("importance", 0.5),
                "cue": c.get("cue", ""),
                "cue_triggered": c["id"] in cue_bonus,
                "stale": bool(c.get("stale")),
                "valid_from": c.get("valid_from", ""),
                "valid_until": c.get("valid_until", ""),
            }
            if c.get("synonym_match"):
                item["synonym_match"] = True
            out.append(item)
    else:
        for c, parts, heat, lexical, prior, score in top:
            out.append({
                "id": c["id"],
                "type": c["type"],
                "confidence": c["confidence"],
                "verified": c["verified"],
                "score": score,
                "heat": heat,
                "abstained": abstained,
                "snippet": _snippet(c.get("content") or "", query),
                "session_id": c.get("session_id", ""),
                "about_peer": c.get("about_peer", ""),
            })
    return out

def conflicts(conn: sqlite3.Connection, ids: list) -> list:
    """Surface candidate contradictions among recalled records (engram /
    remembrane pattern), never auto-merge. Two records conflict when they
    share most of their content terms but one supersedes the other, or a
    CORRECTION names the other's content. The agent judges; we only point.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT id, type, content, superseded_by FROM records WHERE id IN (%s)"
        % ",".join("?" * len(ids)), ids)] if ids else []
    token_sets = {r["id"]: set(_tokens(r["content"])) for r in rows}
    adjudicated = {tuple(sorted((r["a"], r["b"]))) for r in conn.execute(
        "SELECT a, b FROM not_conflict")}
    out = []
    seen = set(adjudicated)
    for a in rows:
        for b in rows:
            if a["id"] == b["id"]:
                continue
            key = tuple(sorted((a["id"], b["id"])))
            if key in seen:
                continue
            ta, tb = token_sets[a["id"]], token_sets[b["id"]]
            if not ta or not tb:
                continue
            overlap = len(ta & tb) / min(len(ta), len(tb))
            linked = a["superseded_by"] == b["id"] or b["superseded_by"] == a["id"]
            correction = (a["type"] == "CORRECTION" and overlap >= 0.5) or \
                         (b["type"] == "CORRECTION" and overlap >= 0.5)
            numeric = (overlap >= 0.6 and _numeric_tokens(a["content"])
                       and _numeric_tokens(b["content"])
                       and _numeric_tokens(a["content"]) != _numeric_tokens(b["content"]))
            if overlap >= 0.6 and (linked or correction or numeric):
                seen.add(key)
                kind = "supersede" if linked else \
                    "correction" if correction else "numeric"
                out.append({"a": a["id"], "b": b["id"],
                            "overlap": round(overlap, 3), "kind": kind})
    return out


def mark_not_conflict(conn: sqlite3.Connection, a: str, b: str) -> dict:
    """Record an adjudication: this pair is compatible, stop surfacing it.

    engram's not_conflict verb: judgment belongs to the agent, and once
    made it persists so the same false positive never costs attention
    twice. Order-independent; idempotent.
    """
    lo, hi = sorted((a, b))
    conn.execute("INSERT OR IGNORE INTO not_conflict (a, b, ts) VALUES (?,?,?)",
                 (lo, hi, now_iso()))
    conn.commit()
    return {"a": lo, "b": hi, "stored": True}


def check_anchors(conn: sqlite3.Connection, apply: bool = False) -> dict:
    """Mark records stale when the file they describe has changed.

    A record may carry anchor_path + anchor_sha (the file content it was
    learned from). When the current sha differs, the lesson may no longer
    hold: flag it for review instead of silently serving it. No competitor
    verified in the scan does checksum-triggered invalidation for coding
    memory; this is the 'memory that knows when it might be wrong' feature.
    """
    import hashlib
    rows = [dict(r) for r in conn.execute(
        "SELECT id, anchor_path, anchor_sha, stale FROM records"
        " WHERE anchor_path != '' AND anchor_sha != ''")]
    checked, flagged, cleared = 0, [], []
    for r in rows:
        checked += 1
        try:
            with open(r["anchor_path"], "rb") as f:
                current = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            current = None  # missing file: the anchor itself is gone
        changed = current != r["anchor_sha"]
        if changed and not r["stale"]:
            flagged.append(r["id"])
            if apply:
                conn.execute("UPDATE records SET stale=1 WHERE id=?", (r["id"],))
        elif not changed and r["stale"]:
            cleared.append(r["id"])
            if apply:
                conn.execute("UPDATE records SET stale=0 WHERE id=?", (r["id"],))
    if apply and (flagged or cleared):
        conn.commit()
    return {"checked": checked, "flagged": flagged, "cleared": cleared,
            "apply": bool(apply)}

def stage_write(conn: sqlite3.Connection, cand: dict) -> int:
    """Park a low-trust candidate for review (hermes write_approval).
    The content hash pins what was reviewed; decide refuses a changed payload.
    Duplicate pending content is not staged twice.
    """
    h = hashlib.sha256(normalize_content(cand["content"]).encode()).hexdigest()
    dup = conn.execute(
        "SELECT id FROM pending_writes WHERE content_hash=? AND status='pending'",
        (h,)).fetchone()
    if dup:
        return dup[0]
    cur = conn.execute(
        "INSERT INTO pending_writes (rtype, content, evidence, confidence, source,"
        " tags, session_id, project_id, event_id, content_hash, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cand["rtype"], cand["content"], cand.get("evidence", ""),
         cand.get("confidence", 0.35), cand.get("source", ""),
         cand.get("tags", ""), cand.get("session_id", ""),
         cand.get("project_id", ""), cand.get("event_id", ""), h, now_iso()))
    conn.commit()
    return cur.lastrowid


def list_pending(conn: sqlite3.Connection, status: str = "pending") -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM pending_writes WHERE status=? ORDER BY created_at", (status,))]


def decide_pending(conn: sqlite3.Connection, pending_id: int, approve: bool) -> dict:
    """Approve -> upsert into records; reject -> mark rejected. The hash is
    re-checked at decision time so an edited payload cannot ride an old approval.
    """
    row = conn.execute("SELECT * FROM pending_writes WHERE id=?",
                       (pending_id,)).fetchone()
    if not row:
        raise KeyError(pending_id)
    row = dict(row)
    if row["status"] != "pending":
        return {"id": pending_id, "status": row["status"], "noop": True}
    h = hashlib.sha256(normalize_content(row["content"]).encode()).hexdigest()
    if h != row["content_hash"]:
        raise ValueError("pending content changed since staging; re-review required")
    if approve:
        rid = upsert_record(
            conn, rtype=row["rtype"], content=row["content"],
            evidence=row["evidence"], verified=0, confidence=row["confidence"],
            source=row["source"], tags=row["tags"], session_id=row["session_id"],
            project_id=row["project_id"], event_id=row["event_id"])
        status = "approved"
    else:
        rid = ""
        status = "rejected"
    conn.execute("UPDATE pending_writes SET status=?, decided_at=? WHERE id=?",
                 (status, now_iso(), pending_id))
    conn.commit()
    return {"id": pending_id, "status": status, "record_id": rid}

def _snippet(content: str, query: str, width: int = SNIPPET_CHARS) -> str:
    """A window onto the first query-term match, not the content's head.

    FTS5's snippet() needs the original MATCH expression, which the rerank
    path no longer has; a local scan over the normalized query terms gives
    the same effect. No match, or content shorter than the window, falls
    back to the head.
    """
    norm = normalize_content(content)
    if len(norm) <= width:
        return norm
    terms = [w for w in normalize_content(query).split() if len(w) >= 4]
    low = norm.lower()
    pos = -1
    for term in terms:
        pos = low.find(term)
        if pos >= 0:
            break
    if pos < 0:
        return norm[: width - 3] + "..."
    start = max(0, pos - 30)
    end = min(len(norm), start + width - 6)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(norm) else ""
    return prefix + norm[start:end] + suffix


def get_record(conn: sqlite3.Connection, record_id: str):
    row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def find_record(conn: sqlite3.Connection, rtype: str, content: str,
                source: str = "", project_id: str = ""):
    """Look up a record by its content-addressed identity."""
    return get_record(conn, record_id_for(rtype, content, source, project_id))


def digest(conn: sqlite3.Connection, budget: int = 2000,
           project_id: str = None, now=None) -> dict:
    """The always-in-context block: top verified/RULE/DECISION records rendered
    into a char-budgeted, deterministic text (hermes-agent frozen snapshot,
    but ranked instead of hand-curated). Stable ordering means host prefix
    caches survive; the budget header tells the agent how full it is.

    L0 of progressive disclosure: one line per record (type, first sentence,
    confidence, id). Full content stays one recall/get away, so session start
    costs ~budget chars instead of the whole store.
    """
    now = now or datetime.now(timezone.utc)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM records WHERE type IN ('RULE','DECISION','FACT','CORRECTION')"
        " AND (verified=1 OR confidence>=0.7) AND superseded_by=''"
        " AND (valid_until='' OR valid_until>?)",
        (now.isoformat(),))]
    if project_id:
        rows = [r for r in rows
                if project_id in (r.get("project_ids") or r.get("project_id") or "")]
    # Verified rules first, then by heat: the block should carry what the
    # agent must not violate before what merely worked once.
    def sort_key(r):
        signals = _record_signals(conn, r["id"])
        h = heat_parts(r, signals, now=now)["heat"]
        tier = 0 if r["type"] == "RULE" and r["verified"] else \
            1 if r["verified"] else 2
        return (tier, -h, r["id"])
    rows.sort(key=sort_key)
    lines = []
    used = 0
    for r in rows:
        first = next((l for l in r["content"].splitlines() if l.strip()), "")
        line = (f"[{r['type']}] {first[:110]} "
                f"(conf {float(r['confidence']):.2f}"
                + (", verified" if r["verified"] else "") + f") id={r['id']}")
        if used + len(line) + 1 > budget:
            # Greedy refill (remembrane knapsack pattern, simplified): a
            # long line no longer fitting must not cut shorter records
            # behind it. Priority order is kept; only oversized lines skip.
            continue
        lines.append(line)
        used += len(line) + 1
    header = f"[memory digest {used}/{budget} chars, {len(lines)} records]"
    return {"text": header + "\n" + "\n".join(lines),
            "chars": used, "budget": budget, "records": len(lines),
            "total_eligible": len(rows)}


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


def _record_signals_bulk(conn: sqlite3.Connection, record_ids: list) -> dict:
    """Signals for many records in one query, keyed by record id.

    recall() reranks a pool of candidates and needs each one's history for
    heat. One query per candidate is an N+1 that dominated rerank time on a
    large corpus (LoCoMo: ~5.9k turns). Chunked to stay under SQLite's
    variable limit.
    """
    out = {rid: [] for rid in record_ids}
    ids = list(dict.fromkeys(record_ids))
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT record_id, signal, source, delta, ts FROM reinforcement"
            f" WHERE record_id IN ({marks}) ORDER BY ts", chunk).fetchall()
        for row in rows:
            out[row["record_id"]].append(dict(row))
    return out


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
    # ACT-R base-level activation (Anderson 1993): B = ln(sum (t - t_i)^-d)
    # approximated by the Park recency power law scaled by recency frequency.
    # Reinforcement count (n) lifts the base level: more accesses = higher
    # activation. This is the one remaining Park-term refinement (item 4
    # of the adopted queue) - kept conservative (0.15 weight on log-count).
    tau = float(row.get("decay_tau") or 0) or HEAT_DECAY_POWER_TAU_DAYS
    recency = (1.0 + age_days / tau) ** -HEAT_DECAY_POWER_BETA

    successes = [s for s in signals if s.get("signal") == "success"]
    n_access = max(1, len(successes) + 1)
    sources = []
    for s in successes:
        name = normalize_content(s.get("source") or "").casefold()
        if name and name not in sources:
            sources.append(name)
    net_delta = sum(float(s.get("delta") or 0.0) for s in signals)
    # Smooth saturating maps instead of hard steps: each additional success
    # and each extra confidence delta still raises the term, but with
    # diminishing returns (Park et al. 2023 normalize each score to [0, 1]
    # before weighting; a step at n=2 or delta=0.2 makes the weight behave
    # as a threshold, not a weight).
    reinforcement = (0.5 * (1.0 - math.exp(-len(successes) / 1.25))
                     + 0.5 * (1.0 - math.exp(-max(0.0, net_delta) / 0.1)))

    confidence = min(1.0, max(0.0, float(row.get("confidence") or 0.0)))
    recency = min(1.0, recency + 0.15 * math.log(n_access) / math.log(10))
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


def purge_fts_free_pages(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS index so deleted content leaves no readable bytes.

    PRAGMA secure_delete scrubs ordinary pages; FTS5 keeps its own storage,
    so a merged index can still hold the text of a deleted row. `optimize`
    rewrites the index from live rows only. Cost is proportional to the
    index, so it is called on the delete paths (forget, forget_cold) rather
    than on every write.
    """
    try:
        conn.execute("INSERT INTO records_fts(records_fts) VALUES('optimize')")
    except sqlite3.Error:
        pass


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
        " AND confidence <= ? AND updated_at < ?"
        " AND id NOT IN (SELECT superseded_by FROM records"
        "                WHERE superseded_by != '')",
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
            purge_fts_free_pages(conn)
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
        "SELECT id, confidence, updated_at, last_decay_at, verified, type,"
        " decay_exempt_until, decay_tau FROM records")]
    for row in rows:
        if row["verified"] or row["type"] in ("RULE", "CORRECTION"):
            continue
        exempt = _parse_ts(row["decay_exempt_until"])
        if exempt and now < exempt:
            continue
        anchors = [t for t in (_parse_ts(row["updated_at"]),
                              _parse_ts(row["last_decay_at"])) if t]
        if not anchors:
            continue
        anchor = max(anchors)
        age_days = max(0.0, (now - anchor).total_seconds() / 86400.0)
        tau = float(row.get("decay_tau") or 0) or DECAY_POWER_TAU_DAYS
        factor = (1.0 + age_days / tau) ** -DECAY_POWER_BETA
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


def _spacing_factor(conn: sqlite3.Connection, record_id: str) -> float:
    """Scale a reinforcement by the gap since the last one (Cepeda 2006).

    Inside SPACING_MIN_GAP_HOURS the repeat is massed and worth ~nothing;
    at SPACING_FULL_GAP_HOURS the gap is full strength; linear between.
    Explicit deltas bypass this: the caller has already decided the size.
    """
    row = conn.execute(
        "SELECT ts FROM reinforcement WHERE record_id=? AND signal IN"
        " ('success','reinforce') ORDER BY ts DESC LIMIT 1", (record_id,)).fetchone()
    if row is None:
        return 1.0
    try:
        last = _parse_ts(row[0])
    except Exception:
        return 1.0
    if last is None:
        return 1.0
    gap_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    if gap_h <= SPACING_MIN_GAP_HOURS:
        return 0.1
    if gap_h >= SPACING_FULL_GAP_HOURS:
        return 1.0
    t = (gap_h - SPACING_MIN_GAP_HOURS) / (SPACING_FULL_GAP_HOURS - SPACING_MIN_GAP_HOURS)
    return round(0.1 + 0.9 * t, 4)


def reinforce(conn: sqlite3.Connection, record_id: str, signal: str,
              delta: float = None, source: str = "",
              content: str = None, evidence: str = None,
              verified: int = 0) -> float:
    """Apply a reinforcement signal, then reconsider promotion on success.

    An explicit delta overrides the canonical one. Promotion runs only for a
    success signal, and its outcome is reported through last_promotion() so the
    caller can say what happened without re-reading the row.

    Negative signals (failure, decay) do not touch updated_at: a record a
    session just discredited must stay eligible for forgetting, not be
    refreshed by the very signal that lowered it.

    Reconsolidation window: a correction with a positive rewrite also
    refreshes updated_at, which resets the decay anchor, so the rewritten
    memory coasts (RECONSOLIDATION_WINDOW_MINUTES documents the intent; the
    window IS the anchor reset, no separate timer). Hypercorrection adds
    decay_exempt_until on top for the grace period.
    """
    if signal not in VALID_SIGNALS:
        raise ValueError(f"invalid signal: {signal}")

    row = conn.execute("SELECT confidence FROM records WHERE id=?",
                       (record_id,)).fetchone()
    if row is None:
        raise ValueError(f"record not found: {record_id}")
    pre_confidence = float(row[0])

    # Reconsolidation gate (Sevenster 2013; Lee et al. 2017): a success
    # recall is read-only reinforcement. Only a correction that carries
    # replacement content opens the rewrite, and Metcalfe & Eich 2019
    # showed a wrong correction to a confidently-wrong record
    # contaminates it deeper, so the rewrite is refused unless the
    # caller marks it verified or supplies evidence.
    rewrite_applied = False
    if signal == "correction" and content is not None:
        if not (verified or (evidence or "").strip()):
            raise ValueError(
                "correction rewrite needs evidence or verified=True")
        if len(content) > REWRITE_MAX_CONTENT_LEN:
            raise ValueError(
                f"content exceeds {REWRITE_MAX_CONTENT_LEN} chars")
        if not content.strip():
            raise ValueError("correction content must not be empty")
        row_content = conn.execute(
            "SELECT content FROM records WHERE id=?", (record_id,)
        ).fetchone()[0]
        if content.strip() != row_content.strip():
            prev = conn.execute(
                "SELECT evidence, verified FROM records WHERE id=?",
                (record_id,)).fetchone()
            conn.execute(
                "UPDATE records SET content=?, evidence=?, verified=?,"
                " updated_at=?, superseded_by='' WHERE id=?",
                (content.strip(),
                 evidence or prev[0] or "",
                 max(1 if verified else 0, prev[1] or 0),
                 now_iso(), record_id))
            rewrite_applied = True

    actual_delta = float(delta) if delta is not None else SIGNAL_DELTAS[signal]
    if delta is None and signal in ("success", "reinforce"):
        actual_delta *= _spacing_factor(conn, record_id)
    # Hypercorrection (B2): the higher the confidence being corrected, the
    # bigger the surprise and the larger the rewrite. An explicit delta is
    # the caller's decision and is not second-guessed. The corrected record
    # also gets a decay grace window so the update settles before the
    # forgetting curve resumes (reconsolidation: a recalled-and-rewritten
    # memory is labile, not immediately re-consolidated).
    grace_iso = ""
    if signal == "correction" and delta is None \
            and pre_confidence >= HYPERCORRECTION_MIN_CONFIDENCE:
        actual_delta *= HYPERCORRECTION_FACTOR
        grace_iso = (datetime.now(timezone.utc)
                     + timedelta(days=HYPERCORRECTION_DECAY_GRACE_DAYS)).isoformat()
    # Atomic clamp in SQL: concurrent reinforces each read the same base, so a
    # Python-side read-modify-write loses every signal but the last.
    if actual_delta >= 0:
        conn.execute(
            "UPDATE records SET confidence=min(1.0, max(0.0, confidence+?)),"
            " updated_at=?, decay_exempt_until=? WHERE id=?",
            (actual_delta, now_iso(), grace_iso, record_id))
    else:
        conn.execute(
            "UPDATE records SET confidence=min(1.0, max(0.0, confidence+?)) WHERE id=?",
            (actual_delta, record_id))
    new_conf = conn.execute("SELECT confidence FROM records WHERE id=?",
                            (record_id,)).fetchone()[0]
    # Memory strength (HLR): a spaced success lengthens this record's own
    # forgetting horizon, a failure shortens it. Massed repeats (spacing
    # factor ~0.1) do not grow strength: cramming is not consolidation.
    if signal in ("success", "reinforce") and actual_delta > 0:
        spaced = delta is not None or _spacing_factor(conn, record_id) >= 0.5
        if spaced:
            conn.execute(
                "UPDATE records SET decay_tau=min(?, max(?, "
                " (CASE WHEN decay_tau>0 THEN decay_tau ELSE ? END) * ?)) WHERE id=?",
                (STRENGTH_TAU_MAX_DAYS, STRENGTH_TAU_MIN_DAYS,
                 DECAY_POWER_TAU_DAYS, STRENGTH_GROWTH, record_id))
    elif signal == "failure":
        conn.execute(
            "UPDATE records SET decay_tau=max(?, "
            " (CASE WHEN decay_tau>0 THEN decay_tau ELSE ? END) * ?) WHERE id=?",
            (STRENGTH_TAU_MIN_DAYS, DECAY_POWER_TAU_DAYS, STRENGTH_SHRINK, record_id))
    conn.execute("""
        INSERT INTO reinforcement (record_id, signal, delta, source, ts)
        VALUES (?, ?, ?, ?, ?)
    """, (record_id, signal, actual_delta, source, now_iso()))
    if rewrite_applied:
        _set_superseded(conn, record_id, source, prev_content=row_content)
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
    backup_paths = glob.glob(os.path.join(BACKUP_DIR, "memory.db.bak.*"))
    backups = len(backup_paths)
    # A restore story needs a recent snapshot: none, or newest older than
    # 7 days, is a warning the operator can act on (run backup or sleep).
    newest = max((os.path.getmtime(p) for p in backup_paths), default=0)
    stale = backups == 0 or (time.time() - newest) > 7 * 86400
    try:
        total = stats(conn).get("total", 0)
    except sqlite3.DatabaseError:
        total = 0
    return {
        "status": "ok" if ok else "corrupt",
        "db_path": db_path_of(conn),
        "backup_count": backups,
        "backup_stale": stale,
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

def export_skill(conn: sqlite3.Connection, record_id: str, out_dir) -> dict:
    """Export a PROCEDURAL/WORKFLOW record (or a reflect cluster) as an
    agentskills.io SKILL.md folder: frontmatter + body + references/ for
    child episodes. Progressive disclosure: L0 name+description routes,
    L1 the body, L2 the evidence files. Description capped at 100 chars
    (hermes lesson: overlong descriptions never route).
    """
    r = get_record(conn, record_id)
    if not r:
        raise KeyError(record_id)
    if r["type"] not in ("PROCEDURAL", "WORKFLOW", "REFLECTIVE", "RULE"):
        raise ValueError(f"skill export needs PROCEDURAL/WORKFLOW/REFLECTIVE/RULE, got {r['type']}")
    out_path = Path(out_dir).expanduser().resolve()
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_content(r["content"])[:40].casefold()).strip("-")
    skill_dir = out_path / (slug or record_id)
    skill_dir.mkdir(parents=True, exist_ok=True)
    first = next((l for l in r["content"].splitlines() if l.strip()), "")
    desc = first[:100]
    body = ["---", f"name: {slug or record_id}", f"description: {desc}", "---", "",
            f"# {first[:80]}", "", r["content"], ""]
    if r["evidence"]:
        body += ["## Evidence", "", r["evidence"], ""]
    children = [dict(c) for c in conn.execute(
        "SELECT id, content, evidence FROM records WHERE parent_id=? ORDER BY created_at",
        (record_id,))]
    refs = []
    if children:
        ref_dir = skill_dir / "references"
        ref_dir.mkdir(exist_ok=True)
        body += ["## Episodes", ""]
        for i, ch in enumerate(children):
            fname = f"episode-{i + 1}.md"
            (ref_dir / fname).write_text(
                ch["content"] + ("\n\nEvidence: " + ch["evidence"] if ch["evidence"] else ""),
                encoding="utf-8")
            refs.append(f"references/{fname}")
            body.append(f"- [{fname}](references/{fname})")
        body.append("")
    (skill_dir / "SKILL.md").write_text("\n".join(body), encoding="utf-8")
    return {"dir": str(skill_dir), "name": slug or record_id,
            "references": refs, "record_id": record_id}


def export_pack(conn: sqlite3.Connection, out_file) -> dict:
    """Git Sync one-file (engram pattern): a compressed, deterministic JSON
    pack of all records, committable to a team repo and importable on
    another machine. Content-addressed ids make the merge deterministic:
    the same lesson from two machines is the same row. No server needed.
    """
    import zlib
    rows = [dict(r) for r in conn.execute(
        "SELECT id, type, content, evidence, verified, confidence, source, tags,"
        " created_at, updated_at, project_id, project_ids, parent_id, event_id,"
        " importance, cue, decay_tau, valid_from, valid_until, anchor_path,"
        " anchor_sha FROM records ORDER BY id")]
    payload = json.dumps({"format": "honcho-pack-1", "records": rows},
                         sort_keys=True, separators=(",", ":")).encode()
    packed = zlib.compress(payload, 9)
    out = Path(out_file).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(packed)
    return {"file": str(out), "records": len(rows), "bytes": len(packed),
            "sha256": hashlib.sha256(packed).hexdigest()}


def import_pack(conn: sqlite3.Connection, in_file) -> dict:
    """Import a pack: upsert by content identity, so re-importing the same
    pack is a no-op and two machines' packs merge without conflicts on
    identical lessons. Confidence takes the max, verified ORs: a
    verification on one machine counts everywhere.
    """
    import zlib
    raw = Path(in_file).expanduser().read_bytes()
    data = json.loads(zlib.decompress(raw))
    if data.get("format") != "honcho-pack-1":
        raise ValueError("not a honcho-pack-1 file")
    imported, merged = 0, 0
    for r in data["records"]:
        existing = get_record(conn, r["id"])
        if not existing:
            # Insert with the pack's id, not a re-derived one: upsert_record
            # hashes raw content, but the pack id was derived from the
            # normalized form, so re-deriving would fork the identity.
            conn.execute(
                "INSERT INTO records (id, type, content, evidence, verified,"
                " confidence, source, tags, created_at, updated_at, project_id,"
                " project_ids, parent_id, event_id, importance, cue, decay_tau,"
                " valid_from, valid_until, anchor_path, anchor_sha)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["type"], r["content"], r.get("evidence", ""),
                 r.get("verified", 0), r.get("confidence", 0.5),
                 r.get("source", ""), r.get("tags", ""),
                 r.get("created_at") or now_iso(), r.get("updated_at") or now_iso(),
                 r.get("project_id", ""), r.get("project_ids", ""),
                 r.get("parent_id", ""), r.get("event_id", ""),
                 r.get("importance", 0.5), r.get("cue", ""),
                 r.get("decay_tau", 0), r.get("valid_from", ""),
                 r.get("valid_until", ""), r.get("anchor_path", ""),
                 r.get("anchor_sha", "")))
            imported += 1
        else:
            conf = max(float(existing["confidence"]), float(r.get("confidence", 0)))
            verified = max(int(existing["verified"]), int(r.get("verified", 0)))
            conn.execute("UPDATE records SET confidence=?, verified=? WHERE id=?",
                         (conf, verified, r["id"]))
            merged += 1
    conn.commit()
    return {"imported": imported, "merged": merged, "total": len(data["records"])}



if __name__ == "__main__":
    conn = connect()
    init_db(conn)
    print(json.dumps({
        "stats": stats(conn),
        "health": health(conn),
    }, indent=2))
    conn.close()
