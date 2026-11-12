"""Untrusted file -> plain text, with type sniffing and resource limits (Phase 6).

Every upload is treated as hostile:
- The type is decided by the file's leading bytes (magic), and must ALSO agree with the
  extension and the allow-list. The client's Content-Type is ignored.
- ``.txt`` / ``.md`` must be valid UTF-8 without NUL bytes (a binary renamed .txt is rejected).
- ``.docx`` is a zip: the central directory is inspected BEFORE anything is decompressed: entry
  count, total declared uncompressed size, per-entry compression ratio (zip bomb), encrypted
  entries, path traversal names, and macros (``vbaProject.bin``) are all rejected. The XML is
  parsed by python-docx (lxml, external entities and network access disabled by default).
- ``.pdf``: encrypted PDFs, PDFs over MAX_PDF_PAGES pages and PDFs whose text exceeds
  MAX_EXTRACTED_CHARS are rejected; extraction stops as soon as the char budget is spent.
- The extracted text is sanitised: Unicode NFC, control and bidi-override characters removed
  (they can hide text from a human reviewer), whitespace runs collapsed.

Nothing here executes content, follows links, or loads images/fonts.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

ZIP_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF-"
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_RATIO = 100  # uncompressed / compressed, per entry
CONTENT_TYPES = {
    "txt": "text/plain",
    "md": "text/markdown",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# C0/C1 controls except \t \n \r, plus bidi overrides / isolates and zero-width characters.
_INVISIBLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_SPACES = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")


class UploadRejected(ValueError):
    """The file is not acceptable. ``status``: 413 too large, 415 wrong type, 422 bad content."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Limits:
    max_bytes: int
    max_pdf_pages: int = 200
    max_chars: int = 2_000_000
    max_docx_uncompressed: int = 30 * 1024 * 1024


@dataclass(frozen=True)
class Extracted:
    kind: str  # txt | md | pdf | docx
    content_type: str
    text: str


def extension(filename: str) -> str:
    """Lower-case extension without the dot ('' if none)."""
    name = PurePosixPath(filename.replace("\\", "/")).name
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def safe_filename(filename: str) -> str:
    """Base name only, printable, at most 120 chars (stored for display; never used as a path)."""
    name = PurePosixPath(filename.replace("\\", "/")).name
    name = _INVISIBLE.sub("", unicodedata.normalize("NFC", name)).strip() or "upload"
    name = re.sub(r"[^\w.\- ()]", "_", name)
    return name[-120:]


def sniff(head: bytes, ext: str, allowed: frozenset[str]) -> str:
    """Decide the kind from magic bytes + extension. Raises UploadRejected(415) on mismatch."""
    if ext not in allowed:
        raise UploadRejected(f"file type .{ext or '?'} is not allowed "
                             f"(allowed: {', '.join(sorted(allowed))})", 415)  # fmt: skip
    if ext == "pdf":
        if not head.startswith(PDF_MAGIC):
            raise UploadRejected("file content is not a PDF", 415)
    elif ext == "docx":
        if not head.startswith(ZIP_MAGIC):
            raise UploadRejected("file content is not a .docx document", 415)
    else:  # txt / md: must not look like a known binary format
        if head.startswith((PDF_MAGIC, ZIP_MAGIC, b"\x7fELF", b"MZ", b"\x89PNG", b"\xff\xd8\xff")):
            raise UploadRejected("file content is binary, not text", 415)
    return ext


def sanitize(text: str, max_chars: int) -> str:
    """NFC, strip invisible/control characters, collapse whitespace. Raises if over budget."""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _INVISIBLE.sub("", text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANKS.sub("\n\n", text).strip()
    if len(text) > max_chars:
        raise UploadRejected(f"document text is too long (over {max_chars} characters)", 413)
    return text


def _text_file(f: BinaryIO, lim: Limits) -> str:
    raw = f.read(lim.max_bytes + 1)
    if b"\x00" in raw:
        raise UploadRejected("text file contains NUL bytes (binary content)", 415)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadRejected("text file is not valid UTF-8", 422) from exc


def _check_zip(f: BinaryIO, lim: Limits) -> None:
    try:
        zf = zipfile.ZipFile(f)
    except zipfile.BadZipFile as exc:
        raise UploadRejected("corrupt .docx (not a valid zip archive)") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise UploadRejected("the .docx has too many parts")
        total = 0
        names = set()
        for i in infos:
            n = i.filename
            if n.startswith("/") or ".." in n.split("/") or "\\" in n:
                raise UploadRejected("the .docx contains an unsafe path")
            if i.flag_bits & 0x1:
                raise UploadRejected("encrypted .docx files are not supported")
            if n.lower().endswith("vbaproject.bin"):
                raise UploadRejected("macro-enabled documents are not accepted")
            total += i.file_size
            if i.file_size > 1024 * 1024 and i.file_size > MAX_ZIP_RATIO * max(i.compress_size, 1):
                raise UploadRejected("the .docx is compressed suspiciously well (zip bomb?)")
            names.add(n)
        if total > lim.max_docx_uncompressed:
            raise UploadRejected("the .docx expands to too much data")
        if "word/document.xml" not in names or "[Content_Types].xml" not in names:
            raise UploadRejected("not a Word document (word/document.xml missing)", 415)
        # Declared sizes can lie: decompress document.xml with a hard cap.
        with zf.open("word/document.xml") as part:
            if len(part.read(lim.max_docx_uncompressed + 1)) > lim.max_docx_uncompressed:
                raise UploadRejected("the .docx expands to too much data")


def _docx(f: BinaryIO, lim: Limits) -> str:
    _check_zip(f, lim)
    f.seek(0)
    import docx  # python-docx

    try:
        d = docx.Document(f)
    except Exception as exc:  # noqa: BLE001 - any parser failure is a bad file
        raise UploadRejected("could not read the .docx document") from exc
    parts: list[str] = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


def _pdf(f: BinaryIO, lim: Limits) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(f, strict=False)
        if reader.is_encrypted:
            raise UploadRejected("encrypted / password-protected PDFs are not supported")
        n = len(reader.pages)
        if n > lim.max_pdf_pages:
            raise UploadRejected(f"PDF has {n} pages (limit {lim.max_pdf_pages})", 413)
        out: list[str] = []
        used = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            used += len(t)
            if used > lim.max_chars * 2:  # raw budget (before whitespace collapse)
                raise UploadRejected("PDF text is too long", 413)
            out.append(t)
    except UploadRejected:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise UploadRejected("could not read the PDF") from exc
    return "\n\n".join(out)


def extract(f: BinaryIO, filename: str, allowed: frozenset[str], lim: Limits) -> Extracted:
    """Sniff, extract and sanitise ``f`` (seekable, positioned anywhere)."""
    f.seek(0)
    head = f.read(8)
    f.seek(0)
    kind = sniff(head, extension(filename), allowed)
    if kind in ("txt", "md"):
        raw = _text_file(f, lim)
    elif kind == "docx":
        raw = _docx(f, lim)
    else:
        raw = _pdf(f, lim)
    text = sanitize(raw, lim.max_chars)
    if len(text) < 20:
        raise UploadRejected("no readable text found in the document"
                             + (" (scanned PDFs without a text layer are not supported)"
                                if kind == "pdf" else ""))  # fmt: skip
    return Extracted(kind=kind, content_type=CONTENT_TYPES[kind], text=text)


def extract_bytes(data: bytes, filename: str, allowed: frozenset[str], lim: Limits) -> Extracted:
    """Convenience for tests."""
    return extract(io.BytesIO(data), filename, allowed, lim)
