"""Generate upload test files in memory (benign and hostile). No binary fixtures in the repo."""

from __future__ import annotations

import io
import zipfile


def text_pdf(lines: list[str]) -> bytes:
    """A minimal valid one-page PDF with a real text layer (hand-built, Helvetica)."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    body = "BT /F1 12 Tf 50 750 Td 14 TL " + " ".join(f"({esc(line)}) '" for line in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(body)} >>\nstream\n{body}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def blank_pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def encrypted_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter()
    for p in PdfReader(io.BytesIO(text_pdf(["secret text inside an encrypted file"]))).pages:
        w.add_page(p)
    w.encrypt("pw-for-test")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    import docx as _docx

    d = _docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for i, row in enumerate(table):
            for j, cell in enumerate(row):
                t.cell(i, j).text = cell
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _rezip(data: bytes, add: dict[str, bytes], *, level: int = zipfile.ZIP_DEFLATED) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", level) as z:
        for i in src.infolist():
            z.writestr(i, src.read(i.filename))
        for name, payload in add.items():
            z.writestr(name, payload)
    return buf.getvalue()


def zip_bomb_docx(mb: int = 40) -> bytes:
    """A real .docx plus one part of ``mb`` MB of zeros (compresses ~1000:1)."""
    return _rezip(docx(["hello"]), {"word/media/bomb.bin": b"\0" * (mb * 1024 * 1024)})


def macro_docx() -> bytes:
    return _rezip(docx(["Quarterly numbers are attached."]), {"word/vbaProject.bin": b"x" * 100})


def traversal_docx() -> bytes:
    return _rezip(docx(["hi there, this is fine text"]), {"../../evil.txt": b"x"})


def zip_not_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "just a zip")
    return buf.getvalue()


EXE = b"MZ\x90\x00" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
