"""Regression tests for memory_context fence handling and provider ABC."""
import pytest

from omp_memory import context as mc


# --- Defect: repeated-line filter drops non-bullet lines and blank lines ----

def test_build_block_keeps_repeated_non_bullet_lines():
    raw = "decision: use sqlite\nreason: local\nreason: local"
    block = mc.build_memory_context_block(raw)
    assert block.count("reason: local") == 2, "dedup dropped a non-bullet line"


def test_build_block_keeps_blank_lines():
    raw = "- first bullet\n\n- second bullet"
    block = mc.build_memory_context_block(raw)
    assert "\n\n" in block, "blank line separating bullets was removed"


def test_build_block_dedups_only_repeated_bullets():
    raw = "- same bullet\n- same bullet\n- other bullet"
    block = mc.build_memory_context_block(raw)
    assert block.count("- same bullet") == 1, "duplicate bullet not collapsed"
    assert "- other bullet" in block


def test_build_block_empty_input_returns_empty():
    assert mc.build_memory_context_block("") == ""
    assert mc.build_memory_context_block("   \n  ") == ""


def test_build_block_wraps_with_fence_and_note():
    block = mc.build_memory_context_block("- fact")
    assert block.startswith("<memory-context>")
    assert block.rstrip().endswith("</memory-context>")
    assert "NOT new user input" in block


# --- Defect: scrubber duplicates text when no tag is present ----------------

def test_scrubber_does_not_duplicate_plain_text():
    s = mc.StreamingContextScrubber()
    out = s.feed("hello world")
    out += s.flush()
    assert out == "hello world", f"scrubber duplicated text: {out!r}"


def test_scrubber_removes_complete_span():
    s = mc.StreamingContextScrubber()
    out = s.feed("before <memory-context>secret</memory-context> after")
    out += s.flush()
    assert "secret" not in out
    assert "before" in out and "after" in out


def test_scrubber_removes_span_split_across_deltas():
    s = mc.StreamingContextScrubber()
    out = ""
    for chunk in ["before <memo", "ry-context>sec", "ret</memory-", "context> after"]:
        out += s.feed(chunk)
    out += s.flush()
    assert "secret" not in out, f"split span leaked: {out!r}"
    assert "before" in out and "after" in out


def test_scrubber_drops_partial_open_tag_tail():
    """A chunk ending mid-tag must not emit the partial tag."""
    s = mc.StreamingContextScrubber()
    out = s.feed("answer <memory-con")
    out += s.flush()
    assert "<memory-con" not in out, f"partial open tag leaked: {out!r}"


def test_scrubber_keeps_unclosed_span_hidden():
    s = mc.StreamingContextScrubber()
    out = s.feed("<memory-context>still streaming")
    out += s.flush()
    assert "still streaming" not in out, "unclosed span leaked its contents"


# --- sanitize_context -------------------------------------------------------

def test_sanitize_removes_blocks_and_tags():
    text = "a <memory-context>x</memory-context> b </memory-context> c"
    cleaned = mc.sanitize_context(text)
    assert "memory-context" not in cleaned
    assert "x" not in cleaned


def test_sanitize_is_idempotent():
    text = "keep <memory-context>drop</memory-context> me"
    once = mc.sanitize_context(text)
    assert mc.sanitize_context(once) == once
