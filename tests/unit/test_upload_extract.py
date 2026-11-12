"""Phase 6: hostile-file handling in app/uploads/extract.py (no database)."""

from __future__ import annotations

import pytest

from app.uploads.extract import Limits, UploadRejected, extract_bytes, safe_filename, sanitize
from tests import upload_files as uf

ALL = frozenset({"txt", "md", "pdf", "docx"})
LIM = Limits(max_bytes=5 * 1024 * 1024, max_pdf_pages=20, max_chars=100_000,
             max_docx_uncompressed=10 * 1024 * 1024)  # fmt: skip
GOOD_TEXT = "Gamma Labs grants 30 days of paid leave per year to every employee."


def rejected(
    data: bytes, name: str, allowed: frozenset[str] = ALL, lim: Limits = LIM
) -> UploadRejected:
    with pytest.raises(UploadRejected) as e:
        extract_bytes(data, name, allowed, lim)
    return e.value


def test_text_markdown_pdf_docx_extract():
    assert extract_bytes(GOOD_TEXT.encode(), "a.txt", ALL, LIM).text == GOOD_TEXT
    md = extract_bytes(b"# Leave\n\n" + GOOD_TEXT.encode(), "a.md", ALL, LIM)
    assert md.kind == "md" and GOOD_TEXT in md.text
    pdf = extract_bytes(uf.text_pdf(["Gamma Labs leave policy", GOOD_TEXT]), "p.pdf", ALL, LIM)
    assert pdf.kind == "pdf" and "30 days of paid leave" in pdf.text
    d = extract_bytes(uf.docx([GOOD_TEXT], [["Band", "Salary"], ["L4", "100"]]), "d.docx", ALL, LIM)
    assert GOOD_TEXT in d.text and "L4 | 100" in d.text


@pytest.mark.parametrize(
    "data,name,status",
    [
        (uf.EXE, "payroll.pdf", 415),  # exe renamed .pdf
        (uf.EXE, "notes.txt", 415),  # exe renamed .txt
        (uf.PNG, "notes.md", 415),
        (uf.text_pdf(["x" * 30]), "x.docx", 415),  # pdf renamed .docx
        (b"%PDF-1.4 hi", "x.txt", 415),  # pdf renamed .txt
        (uf.zip_not_docx(), "x.docx", 415),  # a zip that isn't a Word file
        (b"hello", "script.html", 415),  # not allowed type
        (b"hello", "noextension", 415),
        (b"\x00\x01binary\x00 data that is long enough", "a.txt", 415),
        ("caf\xe9 latin-1 text that is long".encode("latin-1"), "a.txt", 422),
    ],
)
def test_type_confusion_rejected(data, name, status):
    assert rejected(data, name).status == status


def test_allow_list_is_enforced():
    assert (
        rejected(uf.text_pdf(["Long enough text here"]), "a.pdf", frozenset({"txt"})).status == 415
    )


def test_zip_bomb_rejected_without_full_decompression():
    e = rejected(uf.zip_bomb_docx(40), "bomb.docx")
    assert "zip bomb" in e.message or "too much data" in e.message


def test_macro_and_traversal_docx_rejected():
    assert "macro" in rejected(uf.macro_docx(), "m.docx").message
    assert "unsafe path" in rejected(uf.traversal_docx(), "t.docx").message


def test_pdf_limits_encrypted_pages_empty():
    assert "encrypted" in rejected(uf.encrypted_pdf(), "e.pdf").message
    assert rejected(uf.blank_pdf(25), "big.pdf").status == 413
    assert "no readable text" in rejected(uf.blank_pdf(1), "scan.pdf").message
    assert rejected(b"%PDF-1.4\ngarbage that is not a pdf", "bad.pdf").status == 422


def test_text_budget():
    lim = Limits(max_bytes=10_000_000, max_chars=1000)
    assert rejected(("word " * 1000).encode(), "a.txt", ALL, lim).status == 413


def test_sanitize_removes_invisible_and_bidi_characters():
    s = sanitize("Pay\u202eroll\u200b  data\x07\r\n\r\n\r\n\r\nend\ufeff", 1000)
    assert s == "Payroll data\n\nend"


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\Users\\x\\Report Q1.pdf") == "Report Q1.pdf"
    assert safe_filename("<img src=x onerror=1>.pdf") == "_img src_x onerror_1_.pdf"
    assert len(safe_filename("a" * 500 + ".pdf")) == 120
