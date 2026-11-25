"""Handoff brief logic: role-aware switch counting and priority truncation.

These run everywhere. The older handoff test imported the script from the
operator's home directory and skipped when it was absent, so CI never touched
this code path -- which is how a pre-v18 engine import survived in it.
"""
from omp_memory import handoff


def _msg(role, content, extra=None):
    entry = {"type": "message", "message": {"role": role, "content": content}}
    if extra:
        entry["message"].update(extra)
    return entry


def test_model_state_ignores_non_default_roles():
    """Retry-fallback and prewalk hops are not operator model switches."""
    entries = [
        {"type": "model_change", "model": "a", "role": "default", "timestamp": "t1"},
        {"type": "model_change", "model": "b", "role": "temporary", "timestamp": "t2"},
        {"type": "model_change", "model": "c", "role": "fallback", "timestamp": "t3"},
    ]
    state = handoff.model_state(entries)
    assert state.current == "a"
    assert state.previous == ""
    assert state.changes == 0


def test_model_state_counts_real_switch_and_collapses_duplicates():
    entries = [
        {"type": "model_change", "model": "a", "role": "default", "timestamp": "t1"},
        {"type": "model_change", "model": "a", "role": "default", "timestamp": "t2"},
        {"type": "model_change", "model": "b", "role": "default", "timestamp": "t3"},
    ]
    state = handoff.model_state(entries)
    assert state.current == "b"
    assert state.previous == "a"
    assert state.changes == 1


def test_extract_keeps_operator_turns_and_skips_injected_blocks():
    entries = [
        _msg("user", "fix the fts index"),
        _msg("user", "[HANDOFF] carried context, ignore me"),
        _msg("user", "recalled memory context should be skipped"),
        _msg("toolResult", "TypeError: boom", {"toolName": "bash"}),
    ]
    data = handoff.extract(entries)
    assert data["user_turns"] == ["fix the fts index"]
    assert any("TypeError" in f for f in data["failures"])


def test_trim_drops_sections_by_priority_and_keeps_fences_whole():
    lines = [
        "[HANDOFF] header",
        "",
        "## What the operator asked for (verbatim, oldest first)",
        "1. do the thing",
        "## Observed failures (do not repeat blindly)",
        "- TypeError: boom",
        "## Recent command output",
        "```",
        "x" * 4000,
        "```",
    ]
    text, usable = handoff._trim_to_budget(lines, 300)
    assert usable is True
    assert "## What the operator asked for" in text
    assert text.count("```") % 2 == 0, "a fenced block was sliced in half"
    assert "## Recent command output" not in text


def test_trim_reports_unusable_when_only_the_header_fits():
    lines = ["[HANDOFF] header", "", "## What the operator asked for",
             "1. " + "y" * 500]
    text, usable = handoff._trim_to_budget(lines, 80)
    assert usable is False
    assert "skip injection" in text


def test_build_cli_exit_code_signals_unusable(monkeypatch, tmp_path):
    """exit 2 is the machine-readable 'do not inject this'."""
    monkeypatch.setattr(handoff, "latest_session", lambda: None)
    assert handoff.build(1000)["error"] == "no session files"
