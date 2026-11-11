"""Fallback chain, Gemini provider and error-hint parsing (no network, no real keys)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.generation.llm import (
    FakeLLM,
    FallbackLLM,
    GeminiLLM,
    LLMError,
    LLMResponse,
    build_chain,
    gemini_ignores_sampling,
    parse_model_specs,
    parse_retry_after,
    redact_account_ids,
)

FAKE_GEMINI_KEY = "AIza_test_not_a_real_key_0000"


# ---------------- helpers ----------------
class Scripted:
    """LLM stub that raises or answers according to a script, and counts calls."""

    def __init__(self, name: str, outcomes: list[object], provider: str = "groq") -> None:
        self.provider, self.model = provider, name
        self.outcomes = outcomes
        self.calls = 0

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse:
        out = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(out, Exception):
            raise out
        return LLMResponse(text=str(out), model=self.model, provider=self.provider)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ---------------- hint parsing / redaction ----------------
@pytest.mark.parametrize(
    "text,seconds",
    [
        ("tokens per day (TPD): Limit 200000. Please try again in 11m5.712s.", 665.712),
        ("Please try again in 1h2m", 3720.0),
        ("try again in 34s", 34.0),
        ("'retryDelay': '17s'", 17.0),
        ("no hint here", None),
    ],
)
def test_parse_retry_after(text, seconds):
    got = parse_retry_after(text)
    assert got == pytest.approx(seconds) if seconds is not None else got is None


def test_redact_account_ids():
    out = redact_account_ids(
        "org `org_01kyh8ayc1` quota for projects/123456789 and project_number: 98765"
    )
    assert "org_01kyh8ayc1" not in out and "123456789" not in out and "98765" not in out
    assert "org_[redacted]" in out


# ---------------- spec parsing ----------------
def test_parse_model_specs_order_dedupe_and_validation():
    specs = parse_model_specs(
        ("groq", "openai/gpt-oss-120b"),
        " groq:qwen/qwen3.8-27b, gemini:gemini-3.6-flash ,groq:openai/gpt-oss-120b,,",
    )
    assert specs == [
        ("groq", "openai/gpt-oss-120b"),
        ("groq", "qwen/qwen3.8-27b"),
        ("gemini", "gemini-3.6-flash"),
    ]
    with pytest.raises(LLMError, match="bad fallback entry"):
        parse_model_specs(("groq", "m"), "openai:gpt-4")
    with pytest.raises(LLMError, match="bad fallback entry"):
        parse_model_specs(("groq", "m"), "gemini-3.6-flash")


def test_config_rejects_malformed_fallbacks():
    from app.config import ConfigError, Settings

    s = Settings(_env_file=None, llm_provider="groq", llm_fallbacks="nonsense")
    with pytest.raises(ConfigError, match="LLM_FALLBACKS"):
        s.answer_specs()
    assert Settings(_env_file=None, llm_provider="groq", llm_fallbacks="nonsense").answer_specs(
        fallback=False
    ) == (("groq", "openai/gpt-oss-120b"),)


# ---------------- FallbackLLM ----------------
def test_fallback_uses_next_model_and_skips_cooling_model():
    clock = Clock()
    first = Scripted("a", [LLMError("TPD", kind="rate_limit", retry_after_s=100), "from a"])
    second = Scripted("b", ["from b"])
    chain = FallbackLLM([first, second], clock=clock)

    r1 = chain.complete("s", "u")
    assert (r1.text, r1.model) == ("from b", "b") and first.calls == 1

    clock.t += 50  # still cooling down: first model must not be called again
    assert chain.complete("s", "u").model == "b" and first.calls == 1
    assert chain.status()[0]["cooling_down_s"] == 50

    clock.t += 51  # cooled down: the primary is preferred again
    assert chain.complete("s", "u").model == "a" and first.calls == 2


def test_fallback_without_hint_uses_default_cooldown_and_empty_has_none():
    clock = Clock()
    a = Scripted("a", [LLMError("cap", kind="empty"), "ok a"])
    b = Scripted("b", ["ok b"])
    chain = FallbackLLM([a, b], clock=clock)
    assert chain.complete("s", "u").model == "b"
    assert chain.complete("s", "u").model == "a"  # 'empty' is prompt-specific: no cooldown


def test_fallback_all_failed_reports_each_and_soonest_retry():
    clock = Clock()
    a = Scripted("a", [LLMError("TPD", kind="rate_limit", retry_after_s=300)])
    b = Scripted("b", [LLMError("down", kind="server")])  # default 30 s
    chain = FallbackLLM([a, b], clock=clock)
    with pytest.raises(LLMError) as ei:
        chain.complete("s", "u")
    msg = str(ei.value)
    assert "all 2 models failed" in msg and "groq:a rate_limit" in msg and "groq:b server" in msg
    assert ei.value.retry_after_s == pytest.approx(30)

    clock.t += 10  # both cooling down: fail fast without calling either
    with pytest.raises(LLMError, match="cooling down"):
        chain.complete("s", "u")
    assert (a.calls, b.calls) == (1, 1)


def test_fallback_does_not_swallow_non_llm_errors():
    bad = Scripted("a", [ValueError("bug")])
    with pytest.raises(ValueError):
        FallbackLLM([bad, Scripted("b", ["ok"])]).complete("s", "u")


# ---------------- build_chain ----------------
def test_build_chain_skips_fallbacks_without_key_and_requires_primary_key(caplog):
    specs = (("groq", "openai/gpt-oss-120b"), ("gemini", "gemini-3.6-flash"))
    single = build_chain(specs, {"groq": "gsk_test_x", "gemini": ""})
    assert single.provider == "groq"  # gemini skipped -> plain client, not a chain
    assert "GEMINI_API_KEY is empty" in caplog.text and "gsk_test_x" not in caplog.text

    chain = build_chain(specs, {"groq": "gsk_test_x", "gemini": FAKE_GEMINI_KEY})
    assert isinstance(chain, FallbackLLM)
    assert chain.model == "groq:openai/gpt-oss-120b > gemini:gemini-3.6-flash"

    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        build_chain(specs, {"groq": "", "gemini": FAKE_GEMINI_KEY})


def test_build_chain_fake_primary_is_fake_and_never_a_fallback_target():
    assert isinstance(build_chain((("fake", "x"), ("groq", "m")), {}), FakeLLM)


# ---------------- Gemini over a mocked transport ----------------
def _gemini(handler, model="gemini-3.6-flash"):
    return GeminiLLM(
        model,
        FAKE_GEMINI_KEY,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _ok(text="22 days [C1].", finish="STOP"):
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}
            ],
            "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 6},
            "modelVersion": "gemini-3.6-flash",
        },
    )


def test_gemini_request_shape_and_parsing():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        seen["key"] = req.headers.get("x-goog-api-key")
        return _ok(" 22 days [C1]. ")

    out = _gemini(handler).complete("SYS", "USER", max_tokens=512)
    assert (out.text, out.model, out.provider) == ("22 days [C1].", "gemini-3.6-flash", "gemini")
    assert (out.input_tokens, out.output_tokens) == (40, 6)
    b = seen["body"]
    assert ":generateContent" in seen["url"] and "gemini-3.6-flash" in seen["url"]
    assert seen["key"] == FAKE_GEMINI_KEY
    assert b["systemInstruction"]["parts"][0]["text"] == "SYS"
    gc = b["generationConfig"]
    assert gc["maxOutputTokens"] == 512 and gc["seed"] == 7
    # The SDK serialises this nested key in snake_case; Google's proto-JSON parser accepts both.
    tc = gc["thinkingConfig"]
    assert tc.get("thinkingLevel", tc.get("thinking_level")) == "LOW"
    assert "temperature" not in gc  # 3.6+ ignores sampling params; Google says remove them


def test_gemini_older_model_gets_temperature_zero():
    seen: dict = {}

    def handler(req):
        seen["gc"] = json.loads(req.content)["generationConfig"]
        return _ok()

    _gemini(handler, model="gemini-3.5-flash-lite").complete("s", "u")
    assert seen["gc"]["temperature"] == 0
    assert gemini_ignores_sampling("gemini-3.8-flash") and not gemini_ignores_sampling(
        "gemini-3.1-flash-lite"
    )


def test_gemini_rate_limit_maps_with_retry_hint_and_no_key_leak():
    def handler(req):
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Quota exceeded for project_number: 123456789.",
                    "details": [
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"}
                    ],
                }
            },
        )

    with pytest.raises(LLMError) as ei:
        _gemini(handler).complete("s", "u")
    assert ei.value.kind == "rate_limit" and ei.value.retry_after_s == pytest.approx(21)
    assert FAKE_GEMINI_KEY not in str(ei.value) and "123456789" not in str(ei.value)


@pytest.mark.parametrize("status,kind", [(403, "auth"), (400, "bad_request"), (500, "server")])
def test_gemini_error_kinds(status, kind):
    def handler(req):
        return httpx.Response(
            status, json={"error": {"code": status, "message": "x", "status": "X"}}
        )

    with pytest.raises(LLMError) as ei:
        _gemini(handler).complete("s", "u")
    assert ei.value.kind == kind


def test_gemini_empty_answer_at_cap_is_an_error():
    with pytest.raises(LLMError) as ei:
        _gemini(lambda req: _ok(text="", finish="MAX_TOKENS")).complete("s", "u", max_tokens=64)
    assert ei.value.kind == "empty"


def test_gemini_requires_key():
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiLLM("gemini-3.6-flash", "")


# ---------------- eval model columns ----------------
def test_score_row_records_answer_and_judge_model():
    from eval.judge import Verdict
    from eval.run_eval import PipelineOutput, score_row

    class J:
        name = "llm"

        def grade(self, **kw):
            return Verdict(1, "ok", model="openai/gpt-oss-20b")

    q = {
        "id": "x",
        "tenant": "acme",
        "persona": "acme:employee_1",
        "type": "factual",
        "classification": "internal",
        "question": "q",
        "reference_answer": "r",
        "source_doc_ids": ["d"],
    }
    row = score_row(
        q, PipelineOutput("a [C1]", False, ["d"], ["d"], 1.0, model="qwen/qwen3.8-27b"), J()
    )
    assert (row["answer_model"], row["judge_model"]) == ("qwen/qwen3.8-27b", "openai/gpt-oss-20b")


def test_fill_models_from_old_run_meta(tmp_path):
    from eval.run_eval import fill_models

    csv_path = tmp_path / "old.csv"
    csv_path.with_suffix(".summary.json").write_text(
        json.dumps(
            {"meta": {"llm_model": "openai/gpt-oss-120b", "judge_model": "openai/gpt-oss-20b"}}
        )
    )
    row = fill_models({"id": "x", "answer": "a"}, csv_path)
    assert (row["answer_model"], row["judge_model"]) == (
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
    )
    kept = fill_models({"answer_model": "m1", "judge_model": "m2"}, csv_path)
    assert (kept["answer_model"], kept["judge_model"]) == ("m1", "m2")
