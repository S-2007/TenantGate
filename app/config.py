"""Application configuration loaded from environment variables / `.env`.

Rules enforced here:
- Required secrets fail fast with an error that names the missing variable but never echoes a value.
- Every Postgres URL is normalised to the psycopg v3 driver and forced to ``sslmode=require``
  (a weaker sslmode is rejected, not silently upgraded, so misconfiguration is visible).
- The direct (migration) URL must not point at Neon's pooler host.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_WEAK_SSLMODES = {"disable", "allow", "prefer"}


class ConfigError(RuntimeError):
    """Raised when configuration is missing or unsafe. Message never contains secret values."""


def normalize_pg_url(url: str, *, var_name: str) -> str:
    """Return ``url`` rewritten for SQLAlchemy+psycopg3 with ``sslmode=require``.

    Raises ConfigError (naming ``var_name`` only) if the URL is malformed or requests weak TLS.
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql", "postgresql+psycopg"):
        scheme = "postgresql+psycopg"
    else:
        raise ConfigError(f"{var_name} must be a postgresql:// URL")
    if not parts.hostname:
        raise ConfigError(f"{var_name} has no host")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    sslmode = query.get("sslmode")
    if sslmode in _WEAK_SSLMODES:
        raise ConfigError(f"{var_name} uses sslmode={sslmode}; Neon requires sslmode=require")
    if sslmode is None:
        query["sslmode"] = "require"
    # channel_binding is a libpq option psycopg passes through; keep whatever Neon gave us.
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def url_host(url: str) -> str:
    """Hostname of a URL (safe to log: contains no credentials)."""
    return urlsplit(url).hostname or ""


def is_pooler_host(url: str) -> bool:
    """Neon pooled endpoints have ``-pooler`` in the first DNS label."""
    return "-pooler" in url_host(url).split(".")[0]


class Settings(BaseSettings):
    """All runtime settings. Field names map 1:1 to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- database (secrets) ---
    database_url: str = ""
    database_url_direct: str = ""
    test_database_url: str = ""
    test_database_url_direct: str = ""

    # --- LLM ---
    llm_provider: Literal["groq", "gemini", "fake"] = "groq"
    llm_model: str = "openai/gpt-oss-120b"
    # Tried in order when the primary fails (rate limit, outage). "provider:model,provider:model".
    # Empty = no fallback. Entries whose provider key is empty are skipped with a warning.
    llm_fallbacks: str = (
        "groq:qwen/qwen3.8-27b,groq:openai/gpt-oss-20b,"
        "gemini:gemini-3.6-flash,gemini:gemini-3.5-flash-lite"
    )
    # Optional separate judge model (own rate-limit bucket, less self-grading bias).
    # Empty = use LLM_MODEL. The judge uses LLM_PROVIDER.
    judge_model: str = "openai/gpt-oss-20b"
    judge_fallbacks: str = "gemini:gemini-3.5-flash-lite,groq:qwen/qwen3.8-27b"
    groq_api_key: str = ""
    gemini_api_key: str = ""

    # --- auth (used from Phase 2; validated now so startup fails fast) ---
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 15
    jwt_issuer: str = "rag-platform"
    jwt_audience: str = "rag-platform-api"

    # --- retrieval / ingestion ---
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    chunk_size: int = Field(default=500, gt=0)
    chunk_overlap: int = Field(default=50, ge=0)
    top_k: int = Field(default=5, gt=0, le=50)
    # pgvector HNSW: candidates per scan, and iterative scanning so a selective ACL filter still
    # yields top_k rows (D-025). strict_order keeps exact distance order.
    hnsw_ef_search: int = Field(default=100, ge=1, le=1000)
    hnsw_iterative_scan: Literal["off", "strict_order", "relaxed_order"] = "strict_order"
    # App connection pool (pooled Neon URL). Small by default: Neon's pooler multiplexes anyway.
    db_pool_size: int = Field(default=3, ge=1, le=20)
    db_max_overflow: int = Field(default=2, ge=0, le=20)
    corpus_path: str = "data/corpus"

    # --- self-service tenants (Phase 6) ---
    # Registration is OFF unless explicitly enabled. With a REGISTRATION_CODE set, the caller
    # must send it (an invite code for a demo), compared in constant time.
    registration_enabled: bool = False
    registration_code: str = ""
    max_tenants: int = Field(default=50, ge=1, le=10_000)
    # Fixed-window limits per client IP (in-process; see D-042 for the multi-instance caveat).
    register_rate_per_hour: int = Field(default=5, ge=1)
    login_rate_per_minute: int = Field(default=20, ge=1)
    upload_rate_per_minute: int = Field(default=10, ge=1)
    # Per-tenant quotas, enforced inside the SECURITY DEFINER functions (cannot be raced).
    max_users_per_tenant: int = Field(default=50, ge=1)
    max_documents_per_tenant: int = Field(default=200, ge=1)
    max_chunks_per_tenant: int = Field(default=20_000, ge=1)

    # --- invites and sessions (Phase 7) ---
    invite_ttl_hours: int = Field(default=72, ge=1, le=720)
    invite_accept_rate_per_hour: int = Field(default=20, ge=1)  # per IP, preview + accept
    password_change_rate_per_hour: int = Field(default=10, ge=1)

    # --- uploads (Phase 6) ---
    max_upload_mb: float = Field(default=5.0, gt=0, le=50)
    allowed_upload_types: str = "txt,md,pdf,docx"
    upload_roles: str = "admin"  # comma list; who may upload (deny by default: admins only)
    max_pdf_pages: int = Field(default=200, ge=1)
    max_extracted_chars: int = Field(default=2_000_000, ge=1000)
    max_docx_uncompressed_mb: float = Field(default=30.0, gt=0)
    # Extra allowed browser origin for CORS (empty = same-origin only; the bundled UI needs none).
    ui_origin: str = ""

    # --- misc ---
    env: Literal["development", "test", "ci", "production"] = "development"
    store_query_text: bool = True
    neon_api_key: str = ""
    neon_project_id: str = ""

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_lt_size(cls, v: int, info) -> int:  # type: ignore[no-untyped-def]
        size = info.data.get("chunk_size", 500)
        if v >= size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        return v

    # ---------- validation helpers ----------
    def require(self, *names: str) -> None:
        """Raise ConfigError listing every empty variable in ``names`` (names only, no values)."""
        missing = [n.upper() for n in names if not str(getattr(self, n, "") or "").strip()]
        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + ". Fill these in .env (see .env.example for where to get each value)."
            )

    def validate_for_app(self) -> None:
        """Checks run at API startup. Fails closed."""
        required = ["database_url", "jwt_secret"]
        if self.llm_provider in ("groq", "gemini"):
            required.append(f"{self.llm_provider}_api_key")
        self.require(*required)
        self.answer_specs()  # fail fast on a malformed LLM_FALLBACKS entry
        if len(self.jwt_secret) < 32:
            raise ConfigError("JWT_SECRET must be at least 32 characters")
        if self.jwt_algorithm != "HS256":
            # Only the symmetric HS256 path is implemented (RS256: docs/DECISIONS.md D-021).
            raise ConfigError("JWT_ALGORITHM must be HS256")
        if not 1 <= self.jwt_expire_minutes <= 60:
            raise ConfigError("JWT_EXPIRE_MINUTES must be between 1 and 60")
        if not is_pooler_host(self.database_url):
            logger.warning(
                "DATABASE_URL host %s is not a Neon '-pooler' host; the app should use the "
                "pooled connection string",
                url_host(self.database_url),
            )

    def validate_for_migrations(self) -> None:
        """Checks run by Alembic / admin scripts (direct connection only)."""
        self.require("database_url_direct")
        if is_pooler_host(self.database_url_direct):
            raise ConfigError(
                "DATABASE_URL_DIRECT points at a '-pooler' host; migrations must use the "
                "direct (non-pooled) Neon connection string"
            )

    # ---------- Phase 6 helpers ----------
    @property
    def upload_types(self) -> frozenset[str]:
        """Allowed upload extensions (lower case, no dot)."""
        return frozenset(
            t.strip().lower().lstrip(".") for t in self.allowed_upload_types.split(",") if t.strip()
        ) & frozenset({"txt", "md", "pdf", "docx"})

    @property
    def upload_role_set(self) -> frozenset[str]:
        """Roles allowed to upload (unknown role names are ignored: they grant nothing)."""
        from app.auth.policy import KNOWN_ROLES

        return frozenset(r.strip() for r in self.upload_roles.split(",") if r.strip()) & KNOWN_ROLES

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    # ---------- LLM chains ----------
    def answer_specs(self, *, fallback: bool = True) -> tuple[tuple[str, str], ...]:
        """(provider, model) chain for answers: LLM_PROVIDER/LLM_MODEL then LLM_FALLBACKS."""
        from app.generation.llm import LLMError, parse_model_specs

        try:
            specs = parse_model_specs(
                (self.llm_provider, self.llm_model), self.llm_fallbacks if fallback else ""
            )
        except LLMError as exc:
            raise ConfigError(f"LLM_FALLBACKS: {exc}") from exc
        return tuple(specs)

    def judge_specs(self, *, fallback: bool = True) -> tuple[tuple[str, str], ...]:
        """(provider, model) chain for the eval judge: JUDGE_MODEL then JUDGE_FALLBACKS."""
        from app.generation.llm import LLMError, parse_model_specs

        try:
            specs = parse_model_specs(
                (self.llm_provider, self.judge_model or self.llm_model),
                self.judge_fallbacks if fallback else "",
            )
        except LLMError as exc:
            raise ConfigError(f"JUDGE_FALLBACKS: {exc}") from exc
        return tuple(specs)

    def llm_keys(self) -> tuple[tuple[str, str], ...]:
        """Provider -> API key, as a hashable tuple (for the cached chain factory)."""
        return (("groq", self.groq_api_key), ("gemini", self.gemini_api_key))

    # ---------- derived ----------
    @property
    def sqlalchemy_url(self) -> str:
        """Pooled app URL, normalised for SQLAlchemy."""
        return normalize_pg_url(self.database_url, var_name="DATABASE_URL")

    @property
    def sqlalchemy_url_direct(self) -> str:
        """Direct URL, normalised for SQLAlchemy (migrations / admin only)."""
        return normalize_pg_url(self.database_url_direct, var_name="DATABASE_URL_DIRECT")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (call ``get_settings.cache_clear()`` in tests)."""
    return Settings()
