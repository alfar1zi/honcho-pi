"""Five-group measurement harness for honcho-pi's omp_memory.db.

Run: python3 harness.py  -> JSON on stdout, human summary on stderr.

Imports the module under measurement from src/omp_memory (never a copy) with
OMP_MEMORY_DB / OMP_MEMORY_BACKUP_DIR pinned inside bench/db/ before import,
because db.py reads them into module constants at import time.
Groups: A dedup, B retrieval, C token, D staleness, E integrity.
Unmeasurable quantities are null with a reason string; nothing is invented.
"""

import json
import os
import statistics
import sys
import time
import traceback

sys.dont_write_bytecode = True

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH_DIR)
SRC = os.path.join(REPO, "src")
DB_FILE = os.path.join(BENCH_DIR, "db", "bench.db")
BACKUP_DIR = os.path.join(BENCH_DIR, "db", "backups")
DB_MODULE_PATH = os.path.join(SRC, "omp_memory", "db.py")
OUT_DIR = os.path.join(BENCH_DIR, "out")

os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.environ["OMP_MEMORY_DB"] = DB_FILE
os.environ["OMP_MEMORY_BACKUP_DIR"] = BACKUP_DIR
os.environ["OMP_MEMORY_MAX_BACKUPS"] = "0"

sys.path.insert(0, SRC)
sys.path.insert(0, BENCH_DIR)

import dataset  # noqa: E402

# Retrieval depth used for every search call; k for R@k / hit@1 / MRR.
K = 5
# Candidate-window diagnostic: target present anywhere in this wider fetch.
WIN = 100
LATENCY_REPEATS = 3
# Shared operation words, so the token probe returns a full result page.
OPERATION_PROBE = "retention policy limit"


def _md5(path):
    import hashlib
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return None


class Unmeasurable(Exception):
    """Raised by a group when the shipped API cannot express the measurement."""


def load_module():
    """Import omp_memory.db in-place and report which entry points exist.

    Returns (module, api_map). api_map maps each expectation to the actual name
    found, or None. Import failure is reported, not raised: the harness must
    still emit the other groups.
    """
    wanted = ("upsert_record", "remember", "search", "recall", "get_record",
              "stats", "quick_check", "connect", "init_db", "reinforce",
              "list_records")
    try:
        from omp_memory import db as mod
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return None, {"import_error": "%s: %s" % (type(exc).__name__, exc),
                      **{name: None for name in wanted}}
    api = {name: (name if hasattr(mod, name) else None) for name in wanted}
    api["import_error"] = None
    api["db_path_constant"] = getattr(mod, "DB_PATH", None)
    return mod, api


def fresh_conn(mod):
    """Open a connection on the bench database, resetting the file first.

    A reset per measurement keeps group A/B/E independent of each other's rows.
    """
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_FILE + suffix)
        except FileNotFoundError:
            pass
    conn = mod.connect(DB_FILE)
    mod.init_db(conn, DB_FILE)
    return conn


def _ratio(num, den):
    return round(num / den, 4) if den else None


# --- A. dedup ---------------------------------------------------------------

def group_dedup(mod, api):
    """Count rows stored when one fact is written six ways, per write mode.

    Measures whether a write path collapses restatements of one fact:
    units_for_one_fact==1 means casing, whitespace, source and project drift are
    all absorbed. dedup_ratio = rows stored / variants written (lower is better;
    1.0 means nothing merged). Modes are measured separately because they differ
    by design: upsert_record keys on type+content+source+project, remember keys
    on normalised content, and remember(upsert=True) additionally merges a
    near-duplicate that clears the similarity and number guards. A mode whose
    writer or parameter is absent is reported as such, never simulated.
    """
    if mod is None:
        raise Unmeasurable("module not importable")
    variants = dataset.variants()
    result = {"variants_written": len(variants), "modes": {}}

    modes = (("upsert_record", "upsert_record", {}),
             ("remember", "remember", {}),
             ("remember_upsert", "remember", {"upsert": True}))
    for label, fn_name, extra in modes:
        writer = api.get(fn_name) if fn_name else None
        if writer is None:
            result["modes"][label] = {"status": "API TIDAK ADA", "looked_for": fn_name}
            continue
        conn = fresh_conn(mod)
        try:
            try:
                ids, applied, actions = _write_variants(
                    conn, getattr(mod, fn_name), variants, extra)
            except TypeError as exc:
                result["modes"][label] = {
                    "status": "API TIDAK ADA",
                    "looked_for": "%s(%s)" % (fn_name, ", ".join(extra) or ""),
                    "reason": "writer rejects the signature: %s" % exc}
                continue
            total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            cols = _columns(conn)
            result["modes"][label] = {
                "status": "OK",
                "ids": ids,
                "applied_sequence": applied,
                "actions": actions,
                "distinct_ids_returned": len(set(ids)),
                "units_for_one_fact": total,
                "dedup_ratio": _ratio(total, len(variants)),
                "source_values": [r[0] for r in conn.execute(
                    "SELECT DISTINCT source FROM records")],
                # Provenance accumulation exists only where the column exists.
                "project_provenance": (
                    [r[0] for r in conn.execute("SELECT project_ids FROM records ORDER BY 1")]
                    if "project_ids" in cols else None),
                "stored_content": [r[0] for r in conn.execute(
                    "SELECT content FROM records ORDER BY content")],
            }
        finally:
            conn.close()

    result["ceiling"] = (
        "5 of the 6 variants differ only in casing, whitespace or provenance, so "
        "the floor for a content-normalising path is 2 rows: those 5 collapse to "
        "one and the wording-disjoint paraphrase stays its own row unless a "
        "similarity guard merges it. dedup_ratio 1.0 = nothing merged, 0.3333 = "
        "the expected floor for this 6-variant set."
    )
    return result


def _columns(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]


def _write_variants(conn, write, variants, extra=None):
    """Write each variant through the shipped writer; return (ids, applied, actions).

    The writer's keyword signature is inspected rather than assumed, so a renamed
    or dropped parameter surfaces as an API mismatch instead of silently
    distorting the row count. `extra` carries mode-specific flags (upsert=True)
    and is rejected loudly when the writer does not accept it.
    """
    import inspect
    accepted = set(inspect.signature(write).parameters)
    for key in (extra or {}):
        if key not in accepted:
            raise TypeError("%s() has no parameter %r" % (write.__name__, key))
    ids, applied, actions = [], [], []
    for label, content, source, project in variants:
        kwargs = dict(extra or {})
        if "rtype" in accepted:
            kwargs["rtype"] = "FACT"
        if "verified" in accepted:
            kwargs["verified"] = 1
        if "confidence" in accepted:
            kwargs["confidence"] = 0.9
        if "source" in accepted:
            kwargs["source"] = source
        if "project_id" in accepted:
            kwargs["project_id"] = project
        ret = write(conn, content=content, **kwargs)
        if isinstance(ret, dict):
            ids.append(ret.get("id") or ret.get("record_id"))
            applied.append({"variant": label, "applied": ret.get("applied")})
            actions.append({"variant": label, "action": ret.get("action"),
                            "reason": ret.get("reason"), "advisory": ret.get("advisory")})
        else:
            ids.append(ret)
            applied.append({"variant": label, "applied": None})
            actions.append({"variant": label, "action": None})
    return ids, applied, actions


def _distinct(conn, column):
    return [r[0] for r in conn.execute("SELECT DISTINCT %s FROM records ORDER BY 1" % column)]


# --- B. retrieval -----------------------------------------------------------

def group_retrieval(mod, api):
    """R@5, hit@1, MRR, candidate-window hit and latency on synthetic corpora.

    Runs at N=200 and N=2000 for specific (verbatim) and paraphrase queries.
    'win' records whether the target appears in a 100-row fetch, separating a
    ranking failure from an index/recall failure. Latency is min-of-3 per query
    to suppress scheduler noise, then p50/p95 over queries.
    """
    search_name = api.get("recall") or api.get("search")
    if search_name is None or mod is None:
        raise Unmeasurable("no search API found (looked for search, recall)")
    search = getattr(mod, search_name)
    out = {"search_api": search_name, "k": K, "win": WIN,
           "scale": {}, "chance_target_rate": None}

    for n in (200, 2000):
        records, queries = dataset.build(n, seed=1337 + n, subject_style="codename")
        conn = fresh_conn(mod)
        try:
            stored_id = _bulk_insert(mod, conn, records)
            per_kind = {}
            for kind in ("specific", "paraphrase"):
                subset = [q for q in queries if q["kind"] == kind]
                hits, rr, win_hits, lat = 0, 0.0, 0, []
                ranks = []
                for q in subset:
                    want = stored_id[q["target_id"]]
                    best = None
                    for _ in range(LATENCY_REPEATS):
                        s = time.perf_counter()
                        rows = search(conn, q["text"], limit=K)
                        el = (time.perf_counter() - s) * 1000.0
                        best = el if best is None else min(best, el)
                    lat.append(best)
                    ids = [_row_id(r) for r in (rows or [])[:K]]
                    rank = ids.index(want) + 1 if want in ids else None
                    wide = search(conn, q["text"], limit=WIN) or []
                    if want in [_row_id(r) for r in wide]:
                        win_hits += 1
                    ranks.append(rank)
                    if rank is not None:
                        hits += 1
                        rr += 1.0 / rank
                per_kind[kind] = {
                    "queries": len(subset),
                    "recall_at_k": _ratio(hits, len(subset)),
                    "hit_at_1": _ratio(ranks.count(1), len(subset)),
                    "mrr": round(rr / len(subset), 4) if subset else None,
                    "win_rate": _ratio(win_hits, len(subset)),
                    "target_never_found": ranks.count(None),
                    "latency_ms_p50": round(statistics.median(lat), 3) if lat else None,
                    "latency_ms_p95": _p95(lat),
                }
            out["scale"][str(n)] = {
                "records": len(records),
                "type_counts": _count_types(records),
                "chance_hit_at_1": _ratio(1, max(1, n // len(dataset.OPERATIONS))),
                "results": per_kind,
            }
        finally:
            conn.close()
    out["latency_note"] = (
        "p50/p95 are min-of-%d wall-clock samples per query on this host, in-process "
        "SQLite; compare across corpus sizes, not across machines." % LATENCY_REPEATS
    )
    return out


def _row_id(row):
    return row["id"] if isinstance(row, dict) else row[0]


def _p95(values):
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _count_types(records):
    counts = {}
    for r in records:
        counts[r["rtype"]] = counts.get(r["rtype"], 0) + 1
    return counts


def _bulk_insert(mod, conn, records):
    """Insert corpus rows directly (FTS triggers fire) and map corpus id to stored id.

    upsert_record commits per call; measuring retrieval must not pay that cost.
    The stored id is the shipped content-addressed id when record_id_for exists,
    which differs from the generator's ordinal id, so the mapping is returned and
    every ranking comparison goes through it.
    """
    id_for = getattr(mod, "record_id_for", None)
    mapping = {}
    for r in records:
        rid = id_for(r["rtype"], r["content"], r["source"], r["project_id"]) if id_for else r["id"]
        mapping[r["id"]] = rid
        conn.execute(
            "INSERT OR REPLACE INTO records (id,type,content,evidence,verified,confidence,"
            "source,tags,created_at,updated_at,session_id,project_id,parent_id)"
            " VALUES (?,?,?,'',?,?,?,'',?,?,'',?,'')",
            (rid, r["rtype"], r["content"], r["verified"], 0.9 if r["rtype"] == "RULE" else 0.5,
             r["source"], "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", r["project_id"]),
        )
    conn.commit()
    return mapping


# --- C. token ---------------------------------------------------------------

def group_token(mod, api):
    """Payload size of compact vs full result rows under two coarse estimators.

    Compact = id + content + type. Full = every column returned by search.
    Both estimators (chars//4, words*1.35) are rough; they bracket rather than
    predict tokenizer output. Ratio compact/full is the decision-relevant number.
    """
    records, queries = dataset.build(200, seed=99)
    conn = fresh_conn(mod)
    try:
        _bulk_insert(mod, conn, records)
        search_name = api.get("recall") or api.get("search")
        if search_name is None:
            raise Unmeasurable("no search API found (looked for search, recall)")
        # Probe with a shared operation word so the result set is realistic
        # (several rows), not a single verbatim self-match. Token cost is a
        # function of row count and row width, so a 1-row probe measures nothing.
        rows = getattr(mod, search_name)(conn, OPERATION_PROBE, limit=K) or []
        if not rows:
            raise Unmeasurable("search returned no rows for probe %r" % OPERATION_PROBE)
        full_rows = [dict(r) if not isinstance(r, dict) else r for r in rows]
        compact_rows = [{k: r.get(k) for k in ("id", "content", "type")} for r in full_rows]
        full_json = json.dumps(full_rows, sort_keys=True, ensure_ascii=False)
        compact_json = json.dumps(compact_rows, sort_keys=True, ensure_ascii=False)
        return {
            "rows": len(full_rows),
            "full": _size(full_json),
            "compact": _size(compact_json),
            "compact_over_full": _ratio(_est_words(compact_json), _est_words(full_json)),
            "estimators": "len(text)//4 and word_count*1.35; both coarse, no tokenizer run",
            "row_width_full": len(full_json) / max(1, len(full_rows)),
        }
    finally:
        conn.close()


def _size(text):
    return {"chars": len(text), "est_chars_div4": round(len(text) / 4, 1),
            "est_words_x135": _est_words(text)}


def _est_words(text):
    return round(len(text.split()) * 1.35, 1)


# --- D. staleness -----------------------------------------------------------

def group_staleness(mod, api):
    """Does a newer unverified claim displace an older verified one?

    Needs a supersession marker (superseded/supersedes/invalidated column) or a
    recall ranking that prefers verified. Reports which of the two surfaces the
    mechanism could live on, and the observed ordering, without claiming the
    module loses on a criterion it never implemented.
    """
    columns = None
    marker = []
    conn = fresh_conn(mod)
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
        for name in ("superseded", "supersedes", "invalidated", "valid_until",
                     "replaced_by", "superseded_by"):
            if name in columns:
                marker.append(name)
        out = {"columns_probed": ["superseded", "supersedes", "invalidated",
                                  "valid_until", "replaced_by", "superseded_by"],
               "marker_columns_found": marker}

        out["verdict"] = _staleness_verdict(marker)
        writer = api.get("remember") or api.get("upsert_record")
        if writer is None:
            out["writer"] = "API TIDAK ADA"
            out["write_reason"] = "no write API found (looked for remember, upsert_record)"
            out["ranking"] = None
            out["ranking_reason"] = "not measured: cannot write the two conflicting claims"
            return out
        out["writer"] = writer
        write = getattr(mod, writer)
        old_ret = write(conn, "FACT", "relay backlog retention policy is 30 days",
                        verified=1, confidence=0.9, source="ops", project_id="p1")
        new_ret = write(conn, "FACT", "relay backlog retention policy is 60 days",
                        verified=0, confidence=0.5, source="ops", project_id="p1")
        conn.commit()
        old_id = old_ret["id"] if isinstance(old_ret, dict) else old_ret
        new_id = new_ret["id"] if isinstance(new_ret, dict) else new_ret
        out["old_verified_id"] = old_id
        out["new_unverified_id"] = new_id
        out["distinct_rows"] = int(old_id != new_id)

        search_name = api.get("recall") or api.get("search")
        if search_name is None:
            out["ranking"] = None
            out["ranking_reason"] = "no search API found (looked for search, recall)"
        else:
            rows = getattr(mod, search_name)(conn, "relay backlog retention policy days",
                                             limit=K) or []
            order = [_row_id(r) for r in rows]
            ver = {_row_id(r): (r.get("verified") if isinstance(r, dict) else None) for r in rows}
            out["ranking"] = {
                "order": order,
                "verified_flags": ver,
                "old_rank": (order.index(old_id) + 1) if old_id in order else None,
                "new_rank": (order.index(new_id) + 1) if new_id in order else None,
                "verified_wins": bool(old_id in order and
                                      (new_id not in order or order.index(old_id) < order.index(new_id))),
            }
        return out
    finally:
        conn.close()


def _staleness_verdict(marker):
    """One-line statement of whether any supersession surface exists at all."""
    if not marker:
        return ("TIDAK ADA DI POHON: no supersession marker column, so the older "
                "verified claim stays live by construction")
    return "marker present: %s" % ",".join(marker)


# --- E. integrity -----------------------------------------------------------

def group_integrity(mod, api):
    """Row/FTS agreement, quick_check, and survival of RULE/verified rows.

    Corpus is written through the shipped API including RULE and verified rows,
    then compared against records_fts. A gap means the triggers missed a path,
    which silently drops recall for those rows.
    """
    conn = fresh_conn(mod)
    try:
        records, _ = dataset.build(200, seed=7)
        _bulk_insert(mod, conn, records)
        conn.commit()
        n_records = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0]
        n_rule = conn.execute("SELECT COUNT(*) FROM records WHERE type='RULE'").fetchone()[0]
        n_verified = conn.execute("SELECT COUNT(*) FROM records WHERE verified=1").fetchone()[0]
        fts_ids = {r[0] for r in conn.execute("SELECT id FROM records_fts")}
        rec_ids = {r[0] for r in conn.execute("SELECT id FROM records")}
        expected_rule = sum(1 for r in records if r["rtype"] == "RULE")
        expected_verified = sum(1 for r in records if r["verified"] == 1)
        qc = getattr(mod, "quick_check")(conn)
        return {
            "records": n_records,
            "fts": n_fts,
            "records_equal_fts": n_records == n_fts,
            "fts_missing_ids": len(rec_ids - fts_ids),
            "fts_orphan_ids": len(fts_ids - rec_ids),
            "quick_check": "ok" if qc else "FAIL",
            "rule_rows": n_rule,
            "rule_expected": expected_rule,
            "rule_intact": n_rule == expected_rule,
            "verified_rows": n_verified,
            "verified_expected": expected_verified,
            "verified_intact": n_verified == expected_verified,
        }
    finally:
        conn.close()

def group_update_correctness(mod, api):
    """The probe the leaders fail (Zep 8/24 updates, mem0 ADD-only):
    after a correction, does recall serve the NEW fact, is the old one
    gone, and does the system abstain on never-stored facts?
    Synchronous write means freshness latency is ~0 by construction;
    we measure it anyway so the number is evidence, not architecture.
    """
    conn = fresh_conn(mod)
    out = {}
    try:
        write = getattr(mod, "remember")
        reinforce = getattr(mod, "reinforce")
        recall = getattr(mod, "recall")
        old = write(conn, "FACT", "gateway timeout budget is 250 ms under load",
                    verified=1, confidence=0.9, source="ops", project_id="p1")
        old_id = old["id"] if isinstance(old, dict) else old
        t0 = time.perf_counter()
        reinforce(conn, old_id, "correction",
                  content="gateway timeout budget is 1500 ms under load",
                  evidence="load test run 88", source="ops")
        hits = recall(conn, "gateway timeout budget load", limit=3)
        latency_ms = (time.perf_counter() - t0) * 1000
        out["freshness_ms"] = round(latency_ms, 3)
        out["correction_serves_new"] = bool(
            hits and "1500" in (hits[0].get("content") or hits[0].get("snippet") or ""))
        out["old_content_gone"] = all(
            "250 ms" not in (h.get("content") or h.get("snippet") or "") for h in hits)
        ghost = recall(conn, "quixotic zebra furnace calibration", limit=3)
        out["abstains_on_unknown"] = (not ghost) or bool(ghost[0].get("abstained"))
        reinforce(conn, old_id, "correction",
                  content="gateway timeout budget is 3000 ms under load",
                  evidence="load test run 89", source="ops")
        hits2 = recall(conn, "gateway timeout budget load", limit=3)
        out["second_update_wins"] = bool(
            hits2 and "3000" in (hits2[0].get("content") or hits2[0].get("snippet") or ""))
        out["score"] = sum([out["correction_serves_new"], out["old_content_gone"],
                            out["abstains_on_unknown"], out["second_update_wins"]])
        out["score_max"] = 4
        return out
    finally:
        conn.close()


# --- driver -----------------------------------------------------------------

GROUPS = (
    ("A_dedup", group_dedup),
    ("B_retrieval", group_retrieval),
    ("C_token", group_token),
    ("D_staleness", group_staleness),
    ("E_integrity", group_integrity),
    ("F_update_correctness", group_update_correctness),
)


def run():
    """Execute every group, converting API absence into nulls with reasons."""
    md5 = _md5(DB_MODULE_PATH)
    mod, api = load_module()
    report = {
        "harness": "honcho-pi bench",
        "python": "%d.%d.%d" % sys.version_info[:3],
        "measured_module": DB_MODULE_PATH,
        "module_md5": md5,
        "measured_constant_DB_PATH": api.get("db_path_constant"),
        "api": api,
        "fidelity_note": (
            "Retrieval/integrity corpora are inserted with a direct INSERT mirroring "
            "the shipped schema (FTS triggers fire), not through upsert_record, to "
            "avoid per-row commits. Write-path behaviour is measured on upsert_record."
        ),
        "groups": {},
    }
    for name, fn in GROUPS:
        if mod is None:
            report["groups"][name] = {"status": "IMPORT GAGAL", "reason": api["import_error"]}
            continue
        try:
            data = fn(mod, api)
            report["groups"][name] = {"status": "OK", "data": data}
        except Unmeasurable as exc:
            report["groups"][name] = {"status": "API TIDAK ADA", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a group must not kill the run
            report["groups"][name] = {
                "status": "ERROR",
                "reason": "%s: %s" % (type(exc).__name__, exc),
                "traceback": traceback.format_exc(limit=3).splitlines()[-3:],
            }
    return report


def summarize(report, stream=sys.stderr):
    stream.write("[bench] python %s  module md5 %s\n"
                 % (report["python"], report["module_md5"]))
    for name, grp in report["groups"].items():
        stream.write("  %-12s %s" % (name, grp["status"]))
        if grp["status"] != "OK":
            stream.write("  (%s)" % grp.get("reason"))
        stream.write("\n")
    b = report["groups"].get("B_retrieval", {})
    for scale, block in (b.get("data") or {}).get("scale", {}).items():
        for kind, res in block["results"].items():
            stream.write("  B N=%-5s %-10s R@5=%s hit@1=%s MRR=%s win=%s p95=%sms\n"
                         % (scale, kind, res["recall_at_k"], res["hit_at_1"],
                            res["mrr"], res["win_rate"], res["latency_ms_p95"]))
    a = (report["groups"].get("A_dedup", {}).get("data") or {})
    for mode, block in (a.get("modes") or {}).items():
        if block.get("status") == "OK":
            merged = [x["variant"] for x in (block.get("applied_sequence") or [])
                      if x.get("applied") == "updated"]
            stream.write("  A %-16s rows=%s ratio=%s merged=%s\n"
                         % (mode, block["units_for_one_fact"], block["dedup_ratio"],
                            ",".join(merged) or "-"))
        else:
            stream.write("  A %-16s %s (%s)\n"
                         % (mode, block.get("status"), block.get("looked_for")))
    c = report["groups"].get("C_token", {}).get("data")
    if c:
        stream.write("  C compact/full=%s (%s -> %s chars, %s rows)\n"
                     % (c["compact_over_full"], c["full"]["chars"],
                        c["compact"]["chars"], c["rows"]))
    d = report["groups"].get("D_staleness", {}).get("data")
    if d:
        stream.write("  D supersede-marker=%s | ranking=%s\n"
                     % (d.get("marker_columns_found") or "none",
                        (d.get("ranking") or {}).get("verified_wins")))
    e = report["groups"].get("E_integrity", {}).get("data")
    if e:
        stream.write("  E records=%s fts=%s quick_check=%s rule=%s verified=%s\n"
                     % (e["records"], e["fts"], e["quick_check"],
                        "ok" if e["rule_intact"] else "LOST",
                        "ok" if e["verified_intact"] else "LOST"))
    f = report["groups"].get("F_update_correctness", {}).get("data")
    if f:
        stream.write("  F update=%s/4 freshness=%sms (new=%s old_gone=%s abstain=%s second=%s)\n"
                     % (f.get("score"), f.get("freshness_ms"),
                        f.get("correction_serves_new"), f.get("old_content_gone"),
                        f.get("abstains_on_unknown"), f.get("second_update_wins")))


if __name__ == "__main__":
    rep = run()
    summarize(rep)
    json.dump(rep, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
