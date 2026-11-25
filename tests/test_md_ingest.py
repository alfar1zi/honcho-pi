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
