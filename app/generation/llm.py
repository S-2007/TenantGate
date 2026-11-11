"""Thin provider-agnostic LLM wrapper.

``LLMClient.complete(system, user) -> LLMResponse`` is the only surface the app uses.

Providers:
- ``groq``: Groq free tier (openai/gpt-oss-120b, openai/gpt-oss-20b, qwen/qwen3.8-27b) via the
  official ``groq`` SDK, temperature 0, fixed seed, minimal reasoning effort.
- ``gemini``: Google Gemini API free tier (e.g. gemini-3.6-flash, gemini-3.5-flash-lite) via the
  official ``google-genai`` SDK, low thinking level, fixed seed.
- ``fake``: deterministic, offline, no API key. Used by tests and CI. It understands the RAG prompt
  format produced by ``app.generation.prompt`` (numbered ``[C1]`` context blocks + ``Question:``)
  and answers extractively from the best-overlapping sentence, or refuses.

``FallbackLLM`` chains several clients: when one fails (rate limit, outage, empty answer) the next
one is tried, and a rate-limited model is skipped until its retry time instead of being hit again
on every request. ``LLMResponse.model`` always names the model that actually answered.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

REFUSAL_TEXT = "I don't know based on the available documents."

ErrorKind = Literal["rate_limit", "auth", "bad_request", "server", "connection", "empty", "config"]


@dataclass(frozen=True)
class LLMResponse:
    """Text output plus provenance for logging/eval."""

    text: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMError(RuntimeError):
    """Provider call failed. The message never includes the API key.

    ``kind`` lets the fallback chain decide how long to skip a model; ``retry_after_s`` is the
    provider's own retry hint when it gave one.
    """

    def __init__(
        self, message: str, *, kind: ErrorKind = "server", retry_after_s: float | None = None
    ) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.retry_after_s = retry_after_s


class LLMClient(Protocol):
    """Anything that can turn (system, user) into text."""

    provider: str
    model: str

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse: ...


# ---------------------------------------------------------------------------------------------
# Error-message helpers (shared by providers)
# ---------------------------------------------------------------------------------------------
_ACCOUNT_IDS = [
    (re.compile(r"\borg_[A-Za-z0-9]+"), "org_[redacted]"),
    (re.compile(r"(projects?[/ _:=]+(?:number[: =]+)?)\d{4,}", re.IGNORECASE), r"\1[redacted]"),
]
_RETRY_HINT = re.compile(r"(?:try again in|retry in)\s+((?:[\d.]+\s*[hms]\s*)+)", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?([\d.]+)s", re.IGNORECASE)
_DURATION_PART = re.compile(r"([\d.]+)\s*([hms])")


def redact_account_ids(text: str) -> str:
    """Strip provider account / project identifiers from an error message."""
    for pat, repl in _ACCOUNT_IDS:
        text = pat.sub(repl, text)
    return text


def parse_retry_after(text: str) -> float | None:
    """Seconds from hints like 'Please try again in 11m5.7s' or "retryDelay': '34s'"."""
    m = _RETRY_HINT.search(text)
    if m:
        mult = {"h": 3600.0, "m": 60.0, "s": 1.0}
        return sum(float(n) * mult[u] for n, u in _DURATION_PART.findall(m.group(1)))
    m = _RETRY_DELAY.search(text)
    return float(m.group(1)) if m else None


def _provider_message(exc: Exception, limit: int = 400) -> str:
    """Human-readable provider error text, account identifiers redacted, length-capped.

    Prefers the JSON ``error.message`` field over the SDK's stringified dict, so the useful tail
    (limit, used, "try again in ...") survives the length cap.
    """
    body = getattr(exc, "body", None)
    msg = ""
    if isinstance(body, dict):
        err = body.get("error", body)
        msg = str(err.get("message", "")) if isinstance(err, dict) else ""
    if not msg:
        msg = str(getattr(exc, "message", "") or exc)
    return redact_account_ids(msg)[:limit]


# ---------------------------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------------------------
def reasoning_params(model: str) -> dict[str, object]:
    """Per-family Groq request params that keep reasoning-token spend minimal."""
    m = model.lower()
    if "gpt-oss" in m:
        return {"reasoning_effort": "low", "include_reasoning": False}
    if "qwen3" in m:
        return {"reasoning_effort": "none", "reasoning_format": "hidden"}
    return {}


class GroqLLM:
    """Groq chat-completions provider (free tier; OpenAI-compatible API, official ``groq`` SDK).

    ``temperature=0`` + a fixed ``seed`` make answers and judge verdicts as repeatable as the
    provider allows. The SDK retries 429s itself (honouring ``retry-after``) before we give up;
    inside a fallback chain fewer retries are used so a spent daily bucket fails over quickly.

    The free-tier text models are reasoning models (openai/gpt-oss-*, qwen/qwen3*). Their hidden
    reasoning tokens count against max_completion_tokens AND the rate limits, so reasoning is kept
    minimal (``reasoning_effort`` low / none) and not returned (``include_reasoning=False`` /
    ``reasoning_format="hidden"``). An empty answer caused by the token cap raises LLMError
    instead of silently looking like a refusal.
    """

    provider = "groq"

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        temperature: float = 0.0,
        seed: int = 7,
        timeout_s: float = 60.0,
        max_retries: int = 6,
        http_client=None,  # type: ignore[no-untyped-def]  # injectable for tests
    ) -> None:
        import groq  # lazy: tests with the fake provider never import the SDK

        if not api_key:
            raise LLMError("GROQ_API_KEY is empty", kind="config")
        self._groq = groq
        self._client = groq.Groq(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries, http_client=http_client
        )
        self.model = model
        self.temperature = temperature
        self.seed = seed
        self.extra = reasoning_params(model)

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse:
        """Single non-streaming chat completion; returns the assistant message text."""
        g = self._groq
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self.temperature,
                seed=self.seed,
                max_completion_tokens=max_tokens,
                **self.extra,
            )
        except g.AuthenticationError as exc:
            raise LLMError("Groq authentication failed (check GROQ_API_KEY)", kind="auth") from exc
        except g.RateLimitError as exc:
            # Groq's message names the exhausted bucket (RPM/RPD/TPM/TPD) and when to retry.
            msg = _provider_message(exc)
            raise LLMError(
                f"Groq free-tier rate limit exceeded after retries: {msg}",
                kind="rate_limit",
                retry_after_s=parse_retry_after(msg),
            ) from exc
        except g.BadRequestError as exc:
            raise LLMError(
                f"Groq rejected the request: {_provider_message(exc)}", kind="bad_request"
            ) from exc
        except g.APIStatusError as exc:
            raise LLMError(f"Groq API error HTTP {exc.status_code}", kind="server") from exc
        except g.APIConnectionError as exc:  # includes APITimeoutError
            raise LLMError("Could not reach the Groq API", kind="connection") from exc

        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content or "").strip() if choice else ""
        if choice is not None and choice.finish_reason == "length":
            if not text:
                raise LLMError(
                    f"{resp.model} used all {max_tokens} completion tokens (reasoning) "
                    "without producing an answer",
                    kind="empty",
                )
            logger.warning(
                "LLM output truncated at max_tokens=%d (model=%s)", max_tokens, resp.model
            )
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=text,
            model=resp.model,
            provider=self.provider,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )


# ---------------------------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------------------------
_GEMINI_VERSION = re.compile(r"gemini-(\d+)(?:\.(\d+))?")


def gemini_ignores_sampling(model: str) -> bool:
    """Gemini 3.6+ and 4.x ignore temperature/top_p/top_k (Google docs: remove them)."""
    m = _GEMINI_VERSION.search(model.lower())
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    return (major, minor) >= (3, 6)


class GeminiLLM:
    """Google Gemini API provider (free tier) via the official ``google-genai`` SDK.

    Thinking is set to ``low``, the one level every current free-tier flash / flash-lite model
    accepts; thought tokens count toward ``max_output_tokens``, so an empty answer at the cap
    raises LLMError. A fixed seed is sent; temperature 0 is sent only to models that honour it.
    Free-tier note: Google states free-tier content is used to improve its products (see
    docs/DECISIONS.md D-016), so this provider must not receive real customer data.
    """

    provider = "gemini"

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        temperature: float = 0.0,
        seed: int = 7,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        http_client=None,  # type: ignore[no-untyped-def]  # injectable httpx.Client for tests
    ) -> None:
        from google import genai  # lazy: only imported when Gemini is configured
        from google.genai import errors, types

        if not api_key:
            raise LLMError("GEMINI_API_KEY is empty", kind="config")
        self._errors = errors
        self._types = types
        opts: dict[str, object] = {
            "timeout": int(timeout_s * 1000),
            "retry_options": types.HttpRetryOptions(
                attempts=max_retries + 1, http_status_codes=[429, 500, 503]
            ),
        }
        if http_client is not None:
            opts["httpx_client"] = http_client
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(**opts))
        self.model = model
        self.temperature = temperature
        self.seed = seed

    def _config(self, system: str, max_tokens: int):  # type: ignore[no-untyped-def]
        t = self._types
        kw: dict[str, object] = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
            "seed": self.seed,
            "thinking_config": t.ThinkingConfig(thinking_level=t.ThinkingLevel.LOW),
        }
        if not gemini_ignores_sampling(self.model):
            kw["temperature"] = self.temperature
        return t.GenerateContentConfig(**kw)

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse:
        """Single non-streaming generate_content call; returns the response text."""
        import httpx

        e = self._errors
        try:
            resp = self._client.models.generate_content(
                model=self.model, contents=user, config=self._config(system, max_tokens)
            )
        except e.ClientError as exc:
            msg = redact_account_ids(str(exc.message or exc))[:400]
            code = exc.code
            if code == 429:
                raise LLMError(
                    f"Gemini free-tier rate limit exceeded: {msg}",
                    kind="rate_limit",
                    retry_after_s=parse_retry_after(redact_account_ids(str(exc))),
                ) from exc
            if code in (401, 403):
                raise LLMError(
                    "Gemini authentication failed (check GEMINI_API_KEY)", kind="auth"
                ) from exc
            raise LLMError(
                f"Gemini rejected the request (HTTP {code}): {msg}", kind="bad_request"
            ) from exc
        except e.ServerError as exc:
            raise LLMError(f"Gemini API error HTTP {exc.code}", kind="server") from exc
        except e.APIError as exc:
            raise LLMError(f"Gemini API error HTTP {exc.code}", kind="server") from exc
        except httpx.HTTPError as exc:
            raise LLMError("Could not reach the Gemini API", kind="connection") from exc

        text = (resp.text or "").strip()
        cand = resp.candidates[0] if resp.candidates else None
        finish = getattr(getattr(cand, "finish_reason", None), "value", None)
        if not text:
            if finish == "MAX_TOKENS":
                raise LLMError(
                    f"{self.model} used all {max_tokens} output tokens (thinking) "
                    "without producing an answer",
                    kind="empty",
                )
            raise LLMError(f"{self.model} returned no text (finish_reason={finish})", kind="empty")
        if finish == "MAX_TOKENS":
            logger.warning(
                "LLM output truncated at max_tokens=%d (model=%s)", max_tokens, self.model
            )
        usage = resp.usage_metadata
        return LLMResponse(
            text=text,
            model=resp.model_version or self.model,
            provider=self.provider,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
        )


# ---------------------------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------------------------
# How long a model is skipped after each kind of failure, when the provider gave no retry hint.
DEFAULT_COOLDOWN_S: dict[str, float] = {
    "rate_limit": 60.0,
    "auth": 3600.0,  # a bad key will not fix itself
    "bad_request": 600.0,  # usually a model-specific parameter problem
    "server": 30.0,
    "connection": 30.0,
    "config": 3600.0,
    "empty": 0.0,  # specific to this prompt; just try the next model
}
MAX_COOLDOWN_S = 6 * 3600.0


class FallbackLLM:
    """Try each client in order; skip models that recently failed until they cool down.

    Not a load balancer: the first healthy model always answers, so results stay as consistent
    as possible and later models are used only when earlier ones are unavailable.
    Thread-safe (FastAPI runs sync endpoints in a thread pool).
    """

    provider = "fallback"

    def __init__(
        self,
        clients: Sequence[LLMClient],
        *,
        clock=time.monotonic,  # type: ignore[no-untyped-def]
    ) -> None:
        if not clients:
            raise LLMError("FallbackLLM needs at least one client", kind="config")
        self.clients = list(clients)
        self.model = " > ".join(f"{c.provider}:{c.model}" for c in self.clients)
        self._clock = clock
        self._cooldown_until: dict[int, float] = {}
        self._lock = threading.Lock()

    def _available(self, i: int, now: float) -> bool:
        with self._lock:
            return self._cooldown_until.get(i, 0.0) <= now

    def _cool(self, i: int, err: LLMError, now: float) -> float:
        wait = err.retry_after_s if err.retry_after_s is not None else DEFAULT_COOLDOWN_S[err.kind]
        wait = min(max(wait, 0.0), MAX_COOLDOWN_S)
        if wait > 0:
            with self._lock:
                self._cooldown_until[i] = now + wait
        return wait

    def status(self) -> list[dict[str, object]]:
        """Per-model availability (for /health); never includes keys or messages."""
        now = self._clock()
        with self._lock:
            return [
                {
                    "provider": c.provider,
                    "model": c.model,
                    "cooling_down_s": round(max(0.0, self._cooldown_until.get(i, 0.0) - now)),
                }
                for i, c in enumerate(self.clients)
            ]

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse:
        """First successful answer from the chain, or LLMError summarising every failure."""
        failures: list[str] = []
        soonest: float | None = None
        for i, client in enumerate(self.clients):
            now = self._clock()
            name = f"{client.provider}:{client.model}"
            if not self._available(i, now):
                with self._lock:
                    left = self._cooldown_until[i] - now
                failures.append(f"{name} cooling down {left:.0f}s")
                soonest = left if soonest is None else min(soonest, left)
                continue
            try:
                resp = client.complete(system, user, max_tokens=max_tokens)
            except LLMError as err:
                wait = self._cool(i, err, now)
                logger.warning("LLM %s failed (%s); skipping it for %.0fs", name, err.kind, wait)
                failures.append(f"{name} {err.kind}: {str(err)[:160]}")
                if wait > 0:
                    soonest = wait if soonest is None else min(soonest, wait)
                continue
            if i > 0:
                logger.info("LLM fallback: answered by %s (position %d)", name, i + 1)
            return resp
        raise LLMError(
            f"all {len(self.clients)} models failed: " + " | ".join(failures),
            kind="rate_limit",
            retry_after_s=soonest,
        )


# ---------------------------------------------------------------------------------------------
# Fake
# ---------------------------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
_FAKE_STOP = frozenset(
    "a an and are as at be by can do does for from has have how i in is it its many much of on "
    "or our s the their there this to was we what when where which who why will with you your "
    "company policy".split()
)


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _FAKE_STOP and len(w) > 1}


class FakeLLM:
    """Deterministic extractive 'LLM' for tests and CI.

    Answers with the context sentence that shares the most content words with the question and
    cites its block, e.g. ``"Employees get 25 days of leave. [C2]"``. Refuses when overlap is weak.
    """

    provider = "fake"

    def __init__(self, model: str = "fake-extractive-v1", min_overlap: int = 2) -> None:
        self.model = model
        self.min_overlap = min_overlap

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse:
        """Parse the RAG prompt and answer extractively."""
        q_match = re.search(r"^Question:\s*(.+)$", user, re.MULTILINE)
        question = q_match.group(1) if q_match else user
        q_words = _content_words(question)
        best: tuple[int, int, str, str] | None = None  # (overlap, -order, label, sentence)
        order = 0
        for m in re.finditer(
            r"^\[(C\d+)\][^\n]*\n(.*?)(?=^\[C\d+\]|^Question:|\Z)", user, re.MULTILINE | re.DOTALL
        ):
            label, body = m.group(1), m.group(2)
            for sent in re.split(r"(?<=[.!?])\s+|\n+", body):
                sent = sent.strip(" -*#\t")
                if len(sent) < 8:
                    continue
                order += 1
                overlap = len(q_words & _content_words(sent))
                cand = (overlap, -order, label, sent)
                if best is None or cand[:2] > best[:2]:
                    best = cand
        if best is None or best[0] < self.min_overlap:
            text = REFUSAL_TEXT
        else:
            text = f"{best[3]} [{best[2]}]"
        return LLMResponse(text=text, model=self.model, provider=self.provider)


# ---------------------------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------------------------
PROVIDERS = ("groq", "gemini", "fake")
ModelSpec = tuple[str, str]  # (provider, model)


def parse_model_specs(primary: ModelSpec, fallbacks: str) -> list[ModelSpec]:
    """Primary first, then ``"provider:model,provider:model"`` entries; duplicates dropped.

    Raises LLMError(kind="config") naming the bad entry (entries never contain secrets).
    """
    specs: list[ModelSpec] = [primary]
    for raw in (x.strip() for x in fallbacks.split(",")):
        if not raw:
            continue
        provider, sep, model = raw.partition(":")
        provider, model = provider.strip().lower(), model.strip()
        if not sep or provider not in PROVIDERS or not model:
            raise LLMError(
                f"bad fallback entry {raw!r}: use provider:model with provider in {PROVIDERS}",
                kind="config",
            )
        if (provider, model) not in specs:
            specs.append((provider, model))
    return specs


def build_llm(
    provider: str, model: str, api_key: str = "", *, max_retries: int | None = None
) -> LLMClient:
    """Single client for one provider/model."""
    if provider == "fake":
        return FakeLLM()
    if provider == "groq":
        return GroqLLM(
            model=model, api_key=api_key, max_retries=6 if max_retries is None else max_retries
        )
    if provider == "gemini":
        return GeminiLLM(
            model=model, api_key=api_key, max_retries=2 if max_retries is None else max_retries
        )
    raise LLMError(f"Unknown LLM provider {provider!r}", kind="config")


def build_chain(specs: Sequence[ModelSpec], keys: dict[str, str]) -> LLMClient:
    """One client, or a FallbackLLM over every spec whose provider has a key.

    The primary (first) spec must be buildable, otherwise this fails closed. A fallback whose key
    is missing is skipped with a warning that names the variable, never its value.
    """
    if not specs:
        raise LLMError("no LLM configured", kind="config")
    if specs[0][0] == "fake":
        return FakeLLM()
    chained = len(specs) > 1
    retries = 2 if chained else None  # fail over fast instead of waiting out a spent bucket
    clients: list[LLMClient] = []
    for n, (provider, model) in enumerate(specs):
        if provider == "fake":
            continue  # never fall back from a real model to the fake one
        key = keys.get(provider, "")
        if not key and n > 0:
            logger.warning(
                "fallback %s:%s skipped: %s_API_KEY is empty", provider, model, provider.upper()
            )
            continue
        clients.append(build_llm(provider, model, key, max_retries=retries))
    return clients[0] if len(clients) == 1 else FallbackLLM(clients)


@lru_cache(maxsize=8)
def get_llm(provider: str, model: str, api_key: str = "") -> LLMClient:
    """Cached single client per configuration."""
    return build_llm(provider, model, api_key)


@lru_cache(maxsize=8)
def get_chain(specs: tuple[ModelSpec, ...], keys: tuple[tuple[str, str], ...]) -> LLMClient:
    """Cached chain per configuration (keys passed as a tuple so the cache key is hashable)."""
    return build_chain(specs, dict(keys))
