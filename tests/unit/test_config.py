"""Config: fail-fast on missing secrets, URL normalisation, dimension consistency."""

import importlib.util
from pathlib import Path

import pytest

from app.config import ConfigError, Settings, is_pooler_host, normalize_pg_url
from app.db.models import EMBEDDING_DIM as MODEL_DIM

SECRET = "s3cr3t-value-that-must-not-leak"


def _settings(**kw) -> Settings:
    base = dict(_env_file=None, database_url="", jwt_secret="", groq_api_key="")
    base.update(kw)
    return Settings(**base)


def test_missing_secret_names_variable_without_values(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    s = _settings(jwt_secret=SECRET * 2, llm_provider="groq")
    with pytest.raises(ConfigError) as ei:
        s.validate_for_app()
    msg = str(ei.value)
    assert "DATABASE_URL" in msg and "GROQ_API_KEY" in msg
    assert SECRET not in msg


def test_fake_provider_does_not_need_api_key():
    s = _settings(
        database_url="postgresql://u:p@ep-x-pooler.eu.aws.neon.tech/db",
        jwt_secret="x" * 40,
        llm_provider="fake",
    )
    s.validate_for_app()


def test_short_jwt_secret_rejected():
    s = _settings(database_url="postgresql://u:p@h/db", jwt_secret="short", llm_provider="fake")
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        s.validate_for_app()


def test_sslmode_added_and_driver_normalised():
    url = normalize_pg_url("postgresql://u:p@host.neon.tech/db", var_name="X")
    assert url.startswith("postgresql+psycopg://") and "sslmode=require" in url


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_weak_sslmode_rejected(mode):
    with pytest.raises(ConfigError, match="sslmode"):
        normalize_pg_url(f"postgresql://u:{SECRET}@h/db?sslmode={mode}", var_name="DATABASE_URL")


def test_error_message_never_contains_password():
    with pytest.raises(ConfigError) as ei:
        normalize_pg_url(f"mysql://u:{SECRET}@h/db", var_name="DATABASE_URL")
    assert SECRET not in str(ei.value)


def test_direct_url_must_not_be_pooler():
    s = _settings(database_url_direct="postgresql://u:p@ep-abc-pooler.us-east-2.aws.neon.tech/db")
    with pytest.raises(ConfigError, match="pooler"):
        s.validate_for_migrations()


def test_pooler_detection():
    assert is_pooler_host("postgresql://u:p@ep-abc-pooler.us-east-2.aws.neon.tech/db")
    assert not is_pooler_host("postgresql://u:p@ep-abc.us-east-2.aws.neon.tech/db")


def test_overlap_must_be_smaller_than_size():
    with pytest.raises(ValueError):
        _settings(chunk_size=50, chunk_overlap=50)


def test_embedding_dim_in_sync_across_config_model_and_migration(monkeypatch):
    monkeypatch.delenv("EMBEDDING_DIM", raising=False)
    path = Path("migrations/versions/0001_chunks_baseline.py")
    spec = importlib.util.spec_from_file_location("m0001", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert Settings(_env_file=None).embedding_dim == MODEL_DIM == mod.EMBEDDING_DIM


def test_env_example_and_env_list_the_same_variables():
    import re

    def names(p: str) -> set[str]:
        return set(re.findall(r"^([A-Z_]+)=", Path(p).read_text(), re.MULTILINE))

    expected = {
        "DATABASE_URL",
        "DATABASE_URL_DIRECT",
        "TEST_DATABASE_URL",
        "TEST_DATABASE_URL_DIRECT",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "JUDGE_MODEL",
        "GROQ_API_KEY",
        "LLM_FALLBACKS",
        "JUDGE_FALLBACKS",
        "GEMINI_API_KEY",
        "JWT_SECRET",
        "JWT_ALGORITHM",
        "JWT_EXPIRE_MINUTES",
        "JWT_ISSUER",
        "JWT_AUDIENCE",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIM",
        "CHUNK_SIZE",
        "CHUNK_OVERLAP",
        "TOP_K",
        "HNSW_EF_SEARCH",
        "HNSW_ITERATIVE_SCAN",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "CORPUS_PATH",
        "ENV",
        "STORE_QUERY_TEXT",
        "NEON_API_KEY",
        "NEON_PROJECT_ID",
    }
    # Phase 6 settings: documented in .env.example; optional in a local .env because every one
    # has a safe default (registration OFF, uploads admin-only, conservative limits).
    phase6 = {
        "REGISTRATION_ENABLED", "REGISTRATION_CODE", "MAX_TENANTS", "LOGIN_RATE_PER_MINUTE",
        "REGISTER_RATE_PER_HOUR", "UPLOAD_RATE_PER_MINUTE", "MAX_USERS_PER_TENANT",
        "MAX_DOCUMENTS_PER_TENANT", "MAX_CHUNKS_PER_TENANT", "MAX_UPLOAD_MB",
        "ALLOWED_UPLOAD_TYPES", "UPLOAD_ROLES", "MAX_PDF_PAGES", "MAX_EXTRACTED_CHARS",
        "MAX_DOCX_UNCOMPRESSED_MB", "UI_ORIGIN",
        # Phase 7
        "INVITE_TTL_HOURS", "INVITE_ACCEPT_RATE_PER_HOUR", "PASSWORD_CHANGE_RATE_PER_HOUR",
    }  # fmt: skip
    assert names(".env.example") == expected | phase6
    if Path(".env").exists():
        local = names(".env")
        assert expected <= local and local <= expected | phase6, sorted(local ^ expected)
