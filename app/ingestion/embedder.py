"""Embedding interface + implementations.

``Embedder`` is the only thing the rest of the code depends on, so the model can be swapped by
changing EMBEDDING_MODEL (and EMBEDDING_DIM + a migration if the dimension changes).

Implementations:
- ``SentenceTransformerEmbedder``: default (BAAI/bge-small-en-v1.5, 384-d, cosine / normalised).
- ``HashEmbedder``: deterministic lexical hashing embedder (EMBEDDING_MODEL=hash). No download,
  used by tests/CI so they are fast and reproducible. Not for real quality measurements.

Each embedder also exposes a ``Tokenizer`` returning character spans, so the chunker counts tokens
in the *embedding model's own* units and slices the original text (no lossy decode).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from functools import lru_cache
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

Span = tuple[int, int]

BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


@runtime_checkable
class Tokenizer(Protocol):
    """Splits text into tokens, reported as (start_char, end_char) spans into the input."""

    def spans(self, text: str) -> list[Span]: ...


@runtime_checkable
class Embedder(Protocol):
    """Maps text to unit-length vectors of size ``dim``."""

    name: str
    dim: int
    max_tokens: int

    @property
    def tokenizer(self) -> Tokenizer: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class RegexTokenizer:
    """Word / punctuation tokenizer. Deterministic, dependency-free."""

    _pattern = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    def spans(self, text: str) -> list[Span]:
        """Return the character span of every token in ``text``."""
        return [m.span() for m in self._pattern.finditer(text)]


class HFTokenizer:
    """Wraps a HuggingFace *fast* tokenizer and uses its offset mapping."""

    def __init__(self, hf_tokenizer) -> None:  # type: ignore[no-untyped-def]
        if not getattr(hf_tokenizer, "is_fast", False):
            raise ValueError("HFTokenizer requires a fast tokenizer (offset mapping)")
        self._tok = hf_tokenizer

    def spans(self, text: str) -> list[Span]:
        """Return character spans of the model's word-piece tokens (no special tokens)."""
        enc = self._tok(text, add_special_tokens=False, return_offsets_mapping=True, verbose=False)
        return [(s, e) for s, e in enc["offset_mapping"] if e > s]


_STOPWORDS = frozenset(
    "a an and are as at be by for from has have how i in is it its of on or our that the this "
    "to was we what when where which who why will with you your do does did".split()
)


class HashEmbedder:
    """Signed feature-hashing bag-of-words embedder. Deterministic across runs and machines."""

    def __init__(self, dim: int = 384) -> None:
        self.name = "hash"
        self.dim = dim
        self.max_tokens = 10_000
        self._tok = RegexTokenizer()

    @property
    def tokenizer(self) -> Tokenizer:
        return self._tok

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        words = [w for w in re.findall(r"\w+", text.lower()) if w not in _STOPWORDS]
        for w in words:
            h = hashlib.blake2b(w.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            v[idx] += 1.0 if h[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0:
            v[0] = 1.0  # avoid a zero vector (cosine undefined)
            return v
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passages."""
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """Embed a query."""
        return self._vec(text)


class SentenceTransformerEmbedder:
    """sentence-transformers model with normalised embeddings (cosine == dot product)."""

    def __init__(self, model_name: str, dim: int, batch_size: int = 32) -> None:
        from sentence_transformers import SentenceTransformer  # heavy import, keep lazy

        self.name = model_name
        self._model = SentenceTransformer(model_name, device="cpu")
        get_dim = (
            getattr(self._model, "get_embedding_dimension", None)
            or self._model.get_sentence_embedding_dimension
        )
        actual = get_dim()
        if actual != dim:
            raise ValueError(
                f"EMBEDDING_DIM={dim} but model {model_name} produces {actual}-d vectors; "
                "update EMBEDDING_DIM and add a migration"
            )
        self.dim = dim
        self.max_tokens = int(self._model.max_seq_length)
        self.batch_size = batch_size
        self._tok = HFTokenizer(self._model.tokenizer)
        self._query_prefix = BGE_QUERY_INSTRUCTION if "bge" in model_name.lower() else ""

    @property
    def tokenizer(self) -> Tokenizer:
        return self._tok

    def _encode(self, texts: list[str]) -> list[list[float]]:
        arr = self._model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [row.astype(float).tolist() for row in arr]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passages (no instruction prefix)."""
        return self._encode(texts) if texts else []

    def embed_query(self, text: str) -> list[float]:
        """Embed a query (bge models get their retrieval instruction prefix)."""
        return self._encode([self._query_prefix + text])[0]


def build_embedder(model_name: str, dim: int) -> Embedder:
    """Factory: ``hash`` -> HashEmbedder, anything else -> sentence-transformers model."""
    if model_name.lower() == "hash":
        return HashEmbedder(dim)
    return SentenceTransformerEmbedder(model_name, dim)


@lru_cache(maxsize=4)
def get_embedder(model_name: str, dim: int) -> Embedder:
    """Cached embedder per (model, dim) so the model loads once per process."""
    logger.info("loading embedder %s (dim=%d)", model_name, dim)
    return build_embedder(model_name, dim)
