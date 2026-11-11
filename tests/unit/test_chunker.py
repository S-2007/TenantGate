"""Chunker: overlap, no empty chunks, coverage, stable ids."""

import pytest

from app.ingestion.chunker import chunk_id_for, chunk_text
from app.ingestion.embedder import RegexTokenizer

TOK = RegexTokenizer()
TEXT = " ".join(f"w{i}" for i in range(1, 1201))  # 1200 single-token words


def _chunks(text=TEXT, size=500, overlap=50, doc_id="acme-x"):
    return chunk_text(text, doc_id=doc_id, title="T", tokenizer=TOK, size=size, overlap=overlap)


def test_window_sizes_and_overlap():
    cs = _chunks()
    assert [c.n_tokens for c in cs] == [500, 500, 300]
    for a, b in zip(cs, cs[1:], strict=False):
        tail = a.text.split()[-50:]
        head = b.text.split()[:50]
        assert tail == head, "consecutive chunks must share exactly `overlap` tokens"


def test_full_coverage_first_and_last_token():
    cs = _chunks()
    assert cs[0].text.split()[0] == "w1"
    assert cs[-1].text.split()[-1] == "w1200"


def test_no_empty_chunks_and_contiguous_indices():
    messy = "\n\n  # Title  \n\n" + "   \n".join(["para one.", "", "   ", "para two!"]) + "\n\n\n"
    cs = _chunks(messy, size=3, overlap=1)
    assert cs and all(c.text.strip() for c in cs)
    assert [c.chunk_index for c in cs] == list(range(len(cs)))


def test_empty_or_whitespace_document_yields_nothing():
    assert _chunks("") == [] and _chunks("   \n\t ") == []


def test_short_document_single_chunk():
    cs = _chunks("Only a few words here.")
    assert len(cs) == 1 and cs[0].text == "Only a few words here."


def test_ids_are_stable_and_unique():
    a, b = _chunks(), _chunks()
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert len({c.chunk_id for c in a}) == len(a)
    assert a[1].chunk_id == chunk_id_for("acme-x", 1)
    assert _chunks(doc_id="lumen-x")[0].chunk_id != a[0].chunk_id


def test_text_is_exact_slice_preserving_case():
    src = "Mixed CASE Text, with Punctuation!\r\nSecond line."
    cs = _chunks(src, size=100, overlap=10)
    assert cs[0].text == "Mixed CASE Text, with Punctuation!\nSecond line."


@pytest.mark.parametrize("size,overlap", [(0, 0), (10, 10), (10, 11), (10, -1)])
def test_invalid_params(size, overlap):
    with pytest.raises(ValueError):
        _chunks(size=size, overlap=overlap)
