"""Token-window chunker with overlap and deterministic chunk ids.

Tokens are counted with the *embedding model's* tokenizer (via character spans), so a chunk of
CHUNK_SIZE tokens is guaranteed to fit the model's input window and the chunk text is an exact
slice of the source (no lossy detokenisation, original casing/formatting preserved).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.ingestion.embedder import Tokenizer

# Fixed namespace so chunk ids are stable across machines and runs. Never change it: doing so
# would orphan every stored chunk.
CHUNK_NAMESPACE = uuid.UUID("6f6b5a52-1c7e-4b8e-9a43-2f1d5f0c9a10")


def chunk_id_for(doc_id: str, chunk_index: int) -> uuid.UUID:
    """Deterministic id: uuid5(namespace, "<doc_id>#<chunk_index>")."""
    return uuid.uuid5(CHUNK_NAMESPACE, f"{doc_id}#{chunk_index}")


@dataclass(frozen=True)
class TextChunk:
    """A chunk ready to embed and store."""

    chunk_id: uuid.UUID
    doc_id: str
    title: str
    chunk_index: int
    text: str
    n_tokens: int


def chunk_text(
    text: str,
    *,
    doc_id: str,
    title: str,
    tokenizer: Tokenizer,
    size: int,
    overlap: int,
) -> list[TextChunk]:
    """Split ``text`` into windows of ``size`` tokens, consecutive windows sharing ``overlap``.

    Guarantees: no empty/whitespace-only chunks; chunk_index is contiguous from 0; the last
    window always reaches the end of the text; ids depend only on (doc_id, chunk_index).
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be in [0, size)")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    spans = tokenizer.spans(text)
    if not spans:
        return []
    step = size - overlap
    chunks: list[TextChunk] = []
    start = 0
    while True:
        window = spans[start : start + size]
        piece = text[window[0][0] : window[-1][1]].strip()
        if piece:
            idx = len(chunks)
            chunks.append(
                TextChunk(
                    chunk_id=chunk_id_for(doc_id, idx),
                    doc_id=doc_id,
                    title=title,
                    chunk_index=idx,
                    text=piece,
                    n_tokens=len(window),
                )
            )
        if start + size >= len(spans):
            break
        start += step
    return chunks


def embedding_input(chunk: TextChunk) -> str:
    """Text actually embedded: title + chunk (the title disambiguates short chunks)."""
    return f"{chunk.title}\n\n{chunk.text}"
