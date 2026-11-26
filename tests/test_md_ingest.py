"""Ingest omp's builtin markdown memory into the store, incrementally."""
import json
from pathlib import Path

from omp_memory import db, md_ingest


def _write(path: Path, tombstone: bool, entries: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = []
    if tombstone:
        body += [
            "<!-- content moved to honcho: /x/memory.db -->",
            "Records: 100",
            "",
            "Memori di file ini sudah dipindahkan ke database honcho.",
            "Jangan tambah catatan di sini - agent memakai mcp__mem_recall.",
            "",
        ]
    body += [f"- {e}" for e in entries]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def test_entries_after_tombstone_skips_the_notice():
    text = "\n".join([
        "<!-- content moved to honcho: /x -->",
        "Jangan tambah catatan di sini",
        "- " + "a" * 80,
        "- too short",
    ])
    picked = md_ingest.entries_after_tombstone(text)
    assert picked == ["a" * 80]


def test_ingest_writes_only_new_entries_and_is_incremental(tmp_path, monkeypatch):
    root = tmp_path / "memories"
    md = root / "-proj--" / "learned.md"
    _write(md, True, ["first real lesson that is long enough to keep " * 2])
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    conn = db.connect(str(tmp_path / "m.db"))
    db.init_db(conn)

    dry = md_ingest.ingest_md(conn, db, root=root, apply=False)
    assert dry["written"] == 0 and dry["entries"] == 1

    first = md_ingest.ingest_md(conn, db, root=root, apply=True)
    assert first["written"] == 1
    rows = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert rows == 1

    # Same file, unchanged: skipped by signature, no second write.
    again = md_ingest.ingest_md(conn, db, root=root, apply=True)
    assert again["written"] == 0
    assert any(d.get("skipped") == "unchanged" for d in again["details"])

    # Append one entry: only that one is new work.
    with open(md, "a", encoding="utf-8") as fh:
        fh.write("- second lesson appended later and also long enough " + "b" * 40 + "\n")
    third = md_ingest.ingest_md(conn, db, root=root, apply=True)
    assert third["written"] == 2, "unchanged entries are re-sent; upsert dedupes them"
    rows = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert rows == 2, "content identity must prevent duplicates"
    conn.close()


def test_ingest_records_provenance(tmp_path, monkeypatch):
    root = tmp_path / "memories"
    md = root / "-Documents-project-demo--" / "learned.md"
    _write(md, False, ["a lesson with provenance that is long enough " * 2])
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    conn = db.connect(str(tmp_path / "m.db"))
    db.init_db(conn)
    md_ingest.ingest_md(conn, db, root=root, apply=True)
    row = conn.execute("SELECT source, project_id, evidence FROM records").fetchone()
    assert row["source"].startswith("md:")
    assert row["project_id"] == "demo"
    assert "learned.md" in row["evidence"]
    conn.close()


def test_missing_root_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    conn = db.connect(str(tmp_path / "m.db"))
    db.init_db(conn)
    out = md_ingest.ingest_md(conn, db, root=tmp_path / "nope", apply=False)
    assert out["error"] == "memories directory not found"
    conn.close()


def test_mcp_maintenance_ingest_md_calls_the_right_argument_order(tmp_path, monkeypatch):
    """Regression: mcp passed (db, conn) while the signature is (conn, db_mod).

    The op failed with "'sqlite3.Connection' object has no attribute 'DB_PATH'"
    and nothing caught it, because no test exercised the MCP path.
    """
    root = tmp_path / "memories"
    md = root / "-proj--" / "learned.md"
    _write(md, True, ["a lesson ingested through the MCP maintenance op " * 2])
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("OMP_MEMORY_MD_ROOT", str(root))
    from omp_memory import mcp
    out = mcp.tool_maintenance({"op": "ingest_md", "apply": False})
    assert "DB_PATH" not in out, out
    assert '"files": 1' in out


def test_cli_maintenance_ingest_md_works(tmp_path, monkeypatch, capsys):
    root = tmp_path / "memories"
    md = root / "-proj--" / "learned.md"
    _write(md, True, ["another lesson through the CLI maintenance path " * 2])
    monkeypatch.setenv("OMP_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("OMP_MEMORY_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("OMP_MEMORY_MD_ROOT", str(root))
    from omp_memory import cli
    code = cli.main(["maintenance", "ingest_md"])
    out = capsys.readouterr().out
    assert code == 0
    assert "DB_PATH" not in out
    assert '"files": 1' in out


def test_long_entry_is_split_at_sentences():
    """A 2,000-char learn entry must not become one record that sweeps queries."""
    long = " ".join(f"Point {i} about caching layers and pipelines." for i in range(60))
    assert len(long) > md_ingest.MAX_ENTRY_CHARS
    parts = md_ingest._split_long(long)
    assert len(parts) > 1
    for piece in parts:
        assert len(piece) <= md_ingest.MAX_ENTRY_CHARS, len(piece)
        assert len(piece) >= md_ingest.MIN_ENTRY_CHARS
    # No sentence is cut in half: every piece ends like a sentence.
    for piece in parts:
        assert piece.rstrip()[-1] in ".!?;", piece[-30:]


def test_short_entry_is_not_split():
    text = "One short lesson about queues."
    assert md_ingest._split_long(text) == [text]


def test_single_over_long_sentence_is_kept_whole():
    """Truncating prose would corrupt the memory it carries."""
    sentence = "word " * 300 + "."
    parts = md_ingest._split_long(sentence)
    assert parts == [sentence.strip()]


def test_split_does_not_create_runt_fragments():
    text = ". ".join(["A" * 200] * 4) + ". Tail."
    parts = md_ingest._split_long(text)
    assert all(len(p) >= md_ingest.MIN_ENTRY_CHARS for p in parts), [len(p) for p in parts]
