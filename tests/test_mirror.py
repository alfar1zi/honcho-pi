"""Tests for markdown mirror export and git sync."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from omp_memory import db as memory_db


@pytest.fixture(autouse=True)
def _git_test_env(monkeypatch):
    """Ensure git commits in tests do not require global user configuration."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@example.com")


def test_export_is_byte_identical_on_repeat(conn, tmp_path):
    """Repeat export to the same directory produces identical bytes and written == []."""
    memory_db.remember(conn, rtype="FACT", content="Apples are fruit.", confidence=0.9, verified=1)
    memory_db.remember(conn, rtype="RULE", content="Always run tests.", confidence=0.95, verified=1)
    memory_db.remember(conn, rtype="DECISION", content="Use SQLite.", confidence=0.85, verified=0)

    out_dir = tmp_path / "mirror"
    out_dir.mkdir()

    res1 = memory_db.export_markdown(conn, out_dir)
    assert "INDEX.md" in res1["written"]
    assert "fact.md" in res1["written"]
    assert "rule.md" in res1["written"]
    assert "decision.md" in res1["written"]

    bytes1 = {p.name: p.read_bytes() for p in out_dir.iterdir()}
    mtimes1 = {p.name: p.stat().st_mtime_ns for p in out_dir.iterdir()}

    res2 = memory_db.export_markdown(conn, out_dir)
    assert res2["written"] == []
    assert set(res2["unchanged"]) >= {"INDEX.md", "fact.md", "rule.md", "decision.md"}

    bytes2 = {p.name: p.read_bytes() for p in out_dir.iterdir()}
    mtimes2 = {p.name: p.stat().st_mtime_ns for p in out_dir.iterdir()}

    assert bytes1 == bytes2
    assert mtimes1 == mtimes2


def test_export_contains_no_timestamps(conn, tmp_path):
    """Exported markdown files must not contain created_at, updated_at, or ISO timestamps."""
    memory_db.remember(
        conn,
        rtype="FACT",
        content="Stable deterministic fact.",
        confidence=0.9,
        verified=1,
        source="unit_test",
        evidence="Evidence without dates.",
    )

    out_dir = tmp_path / "mirror"
    out_dir.mkdir()
    memory_db.export_markdown(conn, out_dir)

    iso_date_pattern = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
    iso_time_pattern = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")

    for md_file in out_dir.glob("*.md"):
        content = md_file.read_text(encoding="utf-8")
        assert not iso_date_pattern.search(content), f"Timestamp found in {md_file.name}"
        assert not iso_time_pattern.search(content), f"Timestamp found in {md_file.name}"
        assert "created_at" not in content, f"'created_at' found in {md_file.name}"
        assert "updated_at" not in content, f"'updated_at' found in {md_file.name}"


def test_export_refuses_missing_dir(conn, tmp_path):
    """export_markdown raises FileNotFoundError if out_dir does not exist and does not create it."""
    missing = tmp_path / "missing_mirror_dir"
    assert not missing.exists()
    with pytest.raises(FileNotFoundError):
        memory_db.export_markdown(conn, missing)
    assert not missing.exists()


def test_export_refuses_a_file_path(conn, tmp_path):
    """export_markdown raises NotADirectoryError if out_dir points to a regular file."""
    file_path = tmp_path / "regular_file.txt"
    file_path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        memory_db.export_markdown(conn, file_path)


def test_manifest_lists_every_file_with_a_sha256(conn, tmp_path):
    """Manifest records every exported file with a matching sha256 checksum."""
    memory_db.remember(conn, rtype="FACT", content="Fact entry.", confidence=0.8, verified=1)
    memory_db.remember(conn, rtype="RULE", content="Rule entry.", confidence=0.9, verified=0)

    out_dir = tmp_path / "mirror"
    out_dir.mkdir()
    memory_db.export_markdown(conn, out_dir)

    manifest_file = out_dir / ".honcho-mirror.json"
    assert manifest_file.exists()

    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert data.get("version") == 1
    files_map = data.get("files", {})

    assert "INDEX.md" in files_map
    assert "fact.md" in files_map
    assert "rule.md" in files_map
    assert ".honcho-mirror.json" not in files_map

    for relpath, expected_sha in files_map.items():
        target = out_dir / relpath
        assert target.is_file(), f"{relpath} does not exist on disk"
        computed_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        assert computed_sha == expected_sha, f"Hash mismatch for {relpath}"


def test_stale_file_from_old_manifest_is_removed(conn, tmp_path):
    """Files tracked in the old manifest but omitted in the new export are removed."""
    memory_db.remember(conn, rtype="FACT", content="Fact entry.", confidence=0.8, verified=1)
    memory_db.remember(conn, rtype="RULE", content="Rule entry.", confidence=0.9, verified=0)

    out_dir = tmp_path / "mirror"
    out_dir.mkdir()
    memory_db.export_markdown(conn, out_dir)

    rule_file = out_dir / "rule.md"
    assert rule_file.exists()

    res = memory_db.export_markdown(conn, out_dir, types=["FACT"])
    assert not rule_file.exists()
    assert "rule.md" in res["removed"]


def test_file_not_in_manifest_is_preserved(conn, tmp_path):
    """Unmanaged files not recorded in the mirror manifest are preserved across exports."""
    out_dir = tmp_path / "mirror"
    out_dir.mkdir()
    user_file = out_dir / "NOTES.txt"
    user_file.write_text("User personal notes.", encoding="utf-8")

    memory_db.remember(conn, rtype="FACT", content="Some fact.", confidence=0.8, verified=1)
    res = memory_db.export_markdown(conn, out_dir)

    assert user_file.exists()
    assert user_file.read_text(encoding="utf-8") == "User personal notes."
    assert "NOTES.txt" not in res.get("removed", [])


def test_symlink_escape_is_rejected(conn, tmp_path):
    """Symlink target resolving outside out_dir is rejected with ValueError."""
    out_dir = tmp_path / "mirror"
    out_dir.mkdir()
    outside_target = tmp_path / "outside_target.txt"
    outside_target.write_text("escaped content", encoding="utf-8")

    (out_dir / "fact.md").symlink_to(outside_target)
    memory_db.remember(conn, rtype="FACT", content="Should fail due to escape.", confidence=0.9, verified=1)

    with pytest.raises(ValueError):
        memory_db.export_markdown(conn, out_dir)


def test_sync_refuses_non_repo_without_init(tmp_path):
    """sync_mirror raises ValueError if out_dir is not a git repo and init is False."""
    out_dir = tmp_path / "mirror"
    out_dir.mkdir()
    (out_dir / ".honcho-mirror.json").write_text('{"version": 1, "files": {}}', encoding="utf-8")

    with pytest.raises(ValueError):
        memory_db.sync_mirror(out_dir, init=False)


def test_sync_refuses_non_repo_with_init_false_has_no_side_effect(tmp_path):
    """sync_mirror with init=False creates no .git folder when refusing."""
    out_dir = tmp_path / "mirror"
    out_dir.mkdir()
    (out_dir / ".honcho-mirror.json").write_text('{"version": 1, "files": {}}', encoding="utf-8")

    with pytest.raises(ValueError):
        memory_db.sync_mirror(out_dir, init=False)

    assert not (out_dir / ".git").exists()


def test_sync_commits_then_reports_clean(conn, tmp_path):
    """First sync commits changes; second sync reports clean with no commit."""
    memory_db.remember(conn, rtype="FACT", content="Initial memory entry.", confidence=0.9, verified=1)
    out_dir = tmp_path / "mirror"
    out_dir.mkdir()

    memory_db.export_markdown(conn, out_dir)

    res1 = memory_db.sync_mirror(out_dir, init=True)
    assert res1["committed"] is True
    assert isinstance(res1["sha"], str) and len(res1["sha"]) == 40
    assert "fact.md" in res1["files"]

    res2 = memory_db.sync_mirror(out_dir)
    assert res2["committed"] is False
    assert res2["reason"] == "clean"


def test_sync_never_stages_unrelated_files(conn, tmp_path):
    """sync_mirror stages only manifest files, never staging untracked unrelated files."""
    memory_db.remember(conn, rtype="FACT", content="Entry before wild file.", confidence=0.9, verified=1)
    out_dir = tmp_path / "mirror"
    out_dir.mkdir()

    memory_db.export_markdown(conn, out_dir)
    memory_db.sync_mirror(out_dir, init=True)

    wild_file = out_dir / "wild_file.txt"
    wild_file.write_text("Do not commit this file.", encoding="utf-8")

    memory_db.remember(conn, rtype="RULE", content="New rule to commit.", confidence=0.95, verified=1)
    memory_db.export_markdown(conn, out_dir)

    res = memory_db.sync_mirror(out_dir)
    assert res["committed"] is True

    show_out = subprocess.run(
        ["git", "show", "--name-only", "--format=", res["sha"]],
        cwd=out_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert "wild_file.txt" not in show_out
    assert "rule.md" in show_out

    status_out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=out_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "?? wild_file.txt" in status_out


def test_sync_refuses_when_module_lives_inside_out_dir():
    """sync_mirror raises ValueError if omp_memory module directory lives inside out_dir."""
    module_dir = Path(memory_db.__file__).resolve().parent
    with pytest.raises(ValueError):
        memory_db.sync_mirror(module_dir)
    with pytest.raises(ValueError):
        memory_db.sync_mirror(module_dir.parent)


def test_sync_refuses_when_toplevel_differs(tmp_path):
    """sync_mirror raises ValueError when out_dir is a subdirectory of a larger git repo."""
    parent_repo = tmp_path / "parent_repo"
    parent_repo.mkdir()
    subprocess.run(["git", "init"], cwd=parent_repo, check=True, capture_output=True)

    sub_mirror = parent_repo / "sub_mirror"
    sub_mirror.mkdir()
    (sub_mirror / ".honcho-mirror.json").write_text('{"version": 1, "files": {}}', encoding="utf-8")

    with pytest.raises(ValueError):
        memory_db.sync_mirror(sub_mirror, init=False)

    with pytest.raises(ValueError):
        memory_db.sync_mirror(sub_mirror, init=True)
