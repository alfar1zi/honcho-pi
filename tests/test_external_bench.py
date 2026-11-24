"""External benchmark adapter: SHA gate, corpus shape, deterministic metric.

No network, no live DB. The dataset file may be absent on CI: the pure
functions are tested with a tiny in-memory shape, and the SHA gate is tested
against a deliberately wrong digest.
"""
import importlib.util
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "bench" / "external.py"
_spec = importlib.util.spec_from_file_location("bench_external", BENCH)
ext = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ext)


def test_expected_sha_is_pinned():
    assert len(ext.EXPECTED_SHA256) == 64
    assert int(ext.EXPECTED_SHA256, 16) >= 0


def test_sha256_rejects_corrupted_file(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{}")
    assert ext.sha256(f) != ext.EXPECTED_SHA256
    with pytest.raises(ValueError, match="sha256 mismatch"):
        ext.load(f)


def test_load_real_dataset_if_staged():
    p = Path("/tmp/lme_oracle.json")
    if not p.exists():
        pytest.skip("dataset not staged on this host")
    rows = ext.load(p)
    assert len(rows) == 500
    assert "answer_session_ids" in rows[0]
    assert "haystack_sessions" in rows[0]


def test_build_corpus_uses_canonical_gold_label():
    qa = {
        "question_id": "q1",
        "answer_session_ids": ["s2"],
        "haystack_session_ids": ["s1", "s2"],
        "haystack_sessions": [
            [{"role": "user", "content": "no answer here"}],
            [{"role": "user", "content": "gold turn", "has_answer": True},
             {"role": "assistant", "content": "a reply"}],
        ],
    }
    corpus = ext.build_corpus([qa])
    assert len(corpus) == 1
    meta, turns = corpus[0]
    assert meta["question_id"] == "q1"
    # Canonical evaluator indexes every user turn; gold selection happens
    # later via answer_session_ids, not at ingest time.
    assert ("s2", "gold turn", True) in turns
    assert ("s1", "no answer here", False) in turns
