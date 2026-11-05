"""Test TF-IDF cosine similarity edges (replaces jaccard keyword overlap)."""
from omp_memory.web import _tfidf_vectors, _cosine, _similarity_edges


def test_tfidf_common_word_gets_low_weight():
    # "sqlite" muncul di banyak dokumen -> IDF rendah
    docs = [
        "sqlite database sqlite mode",
        "sqlite database backup",
        "react flow graph",
    ]
    vecs = _tfidf_vectors(docs)
    # dokumen 0 dan 1 share "sqlite database" -> similarity tinggi
    s01 = _cosine(vecs[0], vecs[1])
    s02 = _cosine(vecs[0], vecs[2])
    assert s01 > s02, f"s01={s01}, s02={s02}"


def test_tfidf_rare_word_gets_high_weight():
    docs = [
        "wal mode checkpoint",
        "wal mode checkpoint",
        "unrelated topic",
    ]
    vecs = _tfidf_vectors(docs)
    assert _cosine(vecs[0], vecs[1]) > 0.9
    assert _cosine(vecs[0], vecs[2]) == 0.0


def test_tfidf_empty_doc():
    vecs = _tfidf_vectors(["", "hello world"])
    assert _cosine(vecs[0], vecs[1]) == 0.0


def test_similarity_edges_uses_tfidf():
    rows = [
        {"id": "a", "content": "honcho memory recall pipeline"},
        {"id": "b", "content": "honcho memory recall ranking"},
        {"id": "c", "content": "react flow graph rendering"},
    ]
    edges = _similarity_edges(rows)
    ids = {(e["source"], e["target"]) for e in edges}
    assert ("mem-a", "mem-b") in ids or ("mem-b", "mem-a") in ids
    # c gak connect ke a/b
    others = ("mem-a", "mem-b")
    assert not any(
        "mem-c" in (e["source"], e["target"])
        and any(o in (e["source"], e["target"]) for o in others)
        for e in edges
    )
