"""Fake LLM is deterministic, extractive and refuses on weak context."""

from app.generation.llm import REFUSAL_TEXT, FakeLLM

PROMPT = """Context:
[C1] (doc_id=acme-leave, title=Leave)
Full-time staff receive 22 days of annual leave per year. Leave requests go to your manager.
[C2] (doc_id=acme-expense, title=Expenses)
Meals are reimbursed up to $45 per day while travelling.

Question: How many days of annual leave do full-time staff receive?"""


def test_fake_llm_answers_with_citation():
    out = FakeLLM().complete("sys", PROMPT).text
    assert "22 days" in out and out.endswith("[C1]")


def test_fake_llm_is_deterministic():
    llm = FakeLLM()
    assert llm.complete("s", PROMPT).text == llm.complete("s", PROMPT).text


def test_fake_llm_refuses_without_overlap():
    prompt = PROMPT.rsplit("Question:", 1)[0] + "Question: Who won the 1998 chess olympiad?"
    assert FakeLLM().complete("s", prompt).text == REFUSAL_TEXT


# --- Groq provider against a mocked HTTP transport (no network, no key) ---
import json  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.generation.llm import GroqLLM, LLMError  # noqa: E402

FAKE_KEY = "gsk_test_not_a_real_key_123456"


def _groq(handler):
    return GroqLLM(
        "openai/gpt-oss-120b",
        FAKE_KEY,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_groq_sends_deterministic_params_and_parses_text():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": " 22 days [C1]. "},
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
            },
        )

    out = _groq(handler).complete("sys", "user")
    assert out.text == "22 days [C1]." and out.provider == "groq"
    assert (out.input_tokens, out.output_tokens) == (50, 5)
    b = seen["body"]
    assert b["temperature"] == 0 and b["seed"] == 7 and b["model"] == "openai/gpt-oss-120b"
    assert [m["role"] for m in b["messages"]] == ["system", "user"]
    assert b["reasoning_effort"] == "low" and b["include_reasoning"] is False
    assert seen["auth"] == f"Bearer {FAKE_KEY}"


@pytest.mark.parametrize(
    "status,match", [(401, "authentication"), (429, "rate limit"), (500, "HTTP 500")]
)
def test_groq_errors_map_to_llmerror_without_leaking_key(status, match):
    def handler(req):
        return httpx.Response(status, json={"error": {"message": "nope", "type": "x"}})

    with pytest.raises(LLMError, match=match) as ei:
        _groq(handler).complete("s", "u")
    assert FAKE_KEY not in str(ei.value)


def test_groq_requires_key():
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        GroqLLM("m", "")


def test_reasoning_params_per_model_family():
    from app.generation.llm import reasoning_params

    assert reasoning_params("openai/gpt-oss-20b") == {
        "reasoning_effort": "low",
        "include_reasoning": False,
    }
    assert reasoning_params("qwen/qwen3.8-27b") == {
        "reasoning_effort": "none",
        "reasoning_format": "hidden",
    }
    assert reasoning_params("some-plain-model") == {}


def test_groq_empty_answer_at_token_cap_is_an_error_not_a_refusal():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
            },
        )

    with pytest.raises(LLMError, match="completion tokens"):
        _groq(handler).complete("s", "u", max_tokens=64)


def test_rate_limit_message_keeps_retry_hint_and_redacts_org_id():
    msg = (
        "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_01abcXYZ` "
        "service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199000, "
        "Requested 3000. Please try again in 7m12s."
    )

    def handler(req):
        return httpx.Response(429, json={"error": {"message": msg, "type": "tokens"}})

    with pytest.raises(LLMError) as ei:
        _groq(handler).complete("s", "u")
    text = str(ei.value)
    assert "tokens per day (TPD)" in text and "try again in 7m12s" in text
    assert "org_01abcXYZ" not in text and "org_[redacted]" in text
    assert FAKE_KEY not in text
