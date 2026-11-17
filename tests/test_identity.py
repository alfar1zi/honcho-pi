"""Content identity: the same fact must land in one row regardless of surface form.

The surface variants that used to split a fact are casing, whitespace, and the
provenance fields (source, project). A real paraphrase is a different record,
because no threshold separates paraphrase from near-miss reliably.
"""
from omp_memory import db as memory_db


def test_normalize_content_collapses_whitespace_variants():
    assert memory_db.normalize_content("  Port\t=  8080 \n") == "Port = 8080"
    assert memory_db.normalize_content("a\u00a0b") == "a b"
    assert memory_db.normalize_content("") == ""


def test_normalize_content_applies_nfkc():
    # FULLWIDTH digits and a ligature fold to their ASCII forms.
    assert memory_db.normalize_content("\uff10\uff18\uff10\uff18") == "0808"
    assert memory_db.normalize_content("\ufb01le") == "file"


def test_identity_text_casefolds_without_changing_stored_text():
    assert memory_db.identity_text("  PORT = 8080 ") == "port = 8080"
    conn = memory_db.connect(":memory:")
    memory_db.init_db(conn)
    rid = memory_db.remember(conn, "FACT", "PORT = 8080")["id"]
    assert memory_db.get_record(conn, rid)["content"] == "PORT = 8080"
    conn.close()


def test_content_id_ignores_type_source_and_project():
    a = memory_db.content_id("PORT = 8080")
    b = memory_db.content_id("port =   8080")
    assert a == b


def test_six_variants_of_one_fact_collapse_to_one_row(conn):
    base = "Port 8080 must stay bound to loopback in staging"
    variants = [
        base,
        base.upper(),
        "Port   8080 must stay bound to loopback in staging\n",
        base,  # different source
        base,  # different project
    ]
    sources = ["", "", "", "session-capture", "cli"]
    projects = ["", "", "", "", "honcho-pi"]
    ids = [
        memory_db.remember(conn, "FACT", content, source=src, project_id=proj)["id"]
        for content, src, proj in zip(variants, sources, projects)
    ]
    assert len(set(ids)) == 1, f"surface variants split into {len(set(ids))} rows"
    n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n == 1


def test_richest_spelling_wins_and_race_is_idempotent(conn):
    lower = memory_db.remember(conn, "FACT", "port 8080 uses http")
    richer = memory_db.remember(conn, "FACT", "PORT 8080 uses HTTP")
    assert richer["id"] == lower["id"]
    assert memory_db.get_record(conn, richer["id"])["content"] == "PORT 8080 uses HTTP"
    # Same surface again must not flap back to the poorer spelling.
    again = memory_db.remember(conn, "FACT", "port 8080 uses http")
    assert memory_db.get_record(conn, again["id"])["content"] == "PORT 8080 uses HTTP"


def test_provenance_unions_on_merge(conn):
    a = memory_db.remember(conn, "FACT", "one fact", source="s1", project_id="p1")
    memory_db.remember(conn, "FACT", "one fact", source="s2", project_id="p2")
    rec = memory_db.get_record(conn, a["id"])
    assert rec["source"] == "s1,s2", "a second source is provenance, not a rewrite"
    assert rec["project_ids"] == "p1,p2"


def test_repeated_source_does_not_duplicate(conn):
    rid = memory_db.remember(conn, "FACT", "two writes", source="s1")["id"]
    memory_db.remember(conn, "FACT", "two writes", source="s1")
    memory_db.remember(conn, "FACT", "two writes", source="s1")
    assert memory_db.get_record(conn, rid)["source"] == "s1"


def test_content_id_is_stable_across_rtypes(conn):
    a = memory_db.remember(conn, "FACT", "shared statement")
    b = memory_db.remember(conn, "RULE", "shared statement")
    assert a["id"] == b["id"], "identity is content, not type"
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_legacy_rows_survive_migration_and_keep_their_ids(db_path):
    c = memory_db.connect(str(db_path))
    memory_db.init_db(c)
    # Simulate a pre-identity database: old ids, no project_ids column.
    c.executescript("""
        DROP TRIGGER IF EXISTS records_ai;
        DROP TRIGGER IF EXISTS records_ad;
        DROP TRIGGER IF EXISTS records_au;
        ALTER TABLE records DROP COLUMN project_ids;
        DELETE FROM records;
        DELETE FROM schema_version;
        INSERT INTO schema_version(version) VALUES (2);
    """)
    old_id = memory_db.record_id_for("FACT", "legacy lesson", "s", "p")
    ts = memory_db.now_iso()
    c.execute(
        "INSERT INTO records (id,type,content,evidence,verified,confidence,source,tags,"
        "created_at,updated_at,session_id,project_id,parent_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (old_id, "FACT", "legacy lesson", "", 0, 0.5, "s", "", ts, ts, "", "p", ""),
    )
    c.commit()
    c.close()

    c = memory_db.connect(str(db_path))
    memory_db.init_db(c)
    rows = c.execute("SELECT id, content, project_ids FROM records").fetchall()
    assert [r[0] for r in rows] == [old_id], "migration must not rewrite legacy ids"
    assert rows[0]["project_ids"] == "", "new column defaults to empty for old rows"
    assert c.execute("SELECT version FROM schema_version").fetchone()[0] == memory_db.SCHEMA_VERSION
    c.close()


def test_migration_is_idempotent(conn, db_path):
    memory_db.remember(conn, "FACT", "survives remigration")
    c2 = memory_db.connect(str(db_path))
    try:
        memory_db.init_db(c2)
        memory_db.init_db(c2)
        cols = [r[1] for r in c2.execute("PRAGMA table_info(records)").fetchall()]
        assert cols.count("project_ids") == 1
        assert c2.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    finally:
        c2.close()


def test_vector_upsert_contract_is_unchanged(conn):
    """Kept deliberately: project-scoped writes still separate (see Phase 1 note)."""
    a = memory_db.upsert_record(conn, "FACT", "same lesson", session_id="s", project_id="p1")
    b = memory_db.upsert_record(conn, "FACT", "same lesson", session_id="s", project_id="p2")
    assert a != b
    same = memory_db.upsert_record(conn, "FACT", "same lesson", session_id="s", project_id="p1")
    assert same == a


def test_every_exported_name_resolves():
    """__all__ drives star-imports; a stale entry is a silent API break."""
    import omp_memory
    missing = [n for n in omp_memory.__all__ if not hasattr(omp_memory, n)]
    assert not missing, f"__all__ names not defined: {missing}"
    for name in ("content_id", "identity_text", "normalize_content", "remember",
                 "similarity", "decide", "find_similar", "StreamingContextScrubber"):
        assert name in omp_memory.__all__, f"{name} missing from __all__"
