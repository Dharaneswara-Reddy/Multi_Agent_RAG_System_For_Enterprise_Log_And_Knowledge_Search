"""Groq provider: the same LLM surface as `llm.LLMClient`, over Groq's API.

Groq exposes an OpenAI-compatible `/chat/completions` endpoint and has a free
tier, which is the entire reason this exists — the Anthropic path is unchanged
and still selected by default.

## Why httpx rather than the `openai` SDK

Groq is OpenAI-wire-compatible, so `openai` with a `base_url` override would
work. It would also add ~15 MB to an image that runs on a 2 GB Fargate task and
is billed per GB in ECR, to gain retry logic and typed models for exactly two
endpoints. httpx is already a dependency (via anthropic and FastAPI), so this
costs nothing and the request shape stays visible.

## What it deliberately does not do

No streaming. The Anthropic path streams because long reasoning turns risk an
HTTP timeout; Groq's inference is fast enough that a single request completes
well inside the timeout, and a streaming implementation would be code with no
failure it prevents.
"""

from __future__ import annotations

import os
import time
from typing import Any, Literal

import httpx

from aiops.config import settings
from aiops.llm import LLMResult, LLMUnavailable, parse_json_loose
from aiops.observability import tracing as tr

Route = Literal["cheap", "reasoning"]

# Groq's free tier is genuinely free rather than discounted, so every call
# reports $0.00 and the ledger stays truthful. Deliberately not populated with
# invented per-token rates: a fabricated number in the cost column is worse
# than an honest zero, and the accounting machinery works either way. Fill this
# in from Groq's pricing page if the account ever moves to paid.
PRICING: dict[str, tuple[float, float]] = {}


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = PRICING.get(model, (0.0, 0.0))
    return (input_tokens * inp + output_tokens * out) / 1_000_000


class GroqClient:
    """Drop-in replacement for `llm.LLMClient` — same two methods, same result type."""

    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key or os.getenv("GROQ_API_KEY")
        self._client: httpx.Client | None = None

    @property
    def available(self) -> bool:
        # Unlike Anthropic, there is no profile or federation fallback to
        # consider: a key is the only credential Groq accepts, so its absence
        # is conclusive.
        return bool(self._key)

    def _http(self) -> httpx.Client:
        if self._client is None:
            if not self._key:
                raise LLMUnavailable(
                    "GROQ_API_KEY is not set. Get a free key at https://console.groq.com/keys "
                    "and set AIOPS_LLM_PROVIDER=groq."
                )
            self._client = httpx.Client(
                base_url=settings.groq_base_url,
                headers={"Authorization": f"Bearer {self._key}"},
                # Generous: a cold 70B model on a busy free tier can take a
                # while to return the first token, and there is no streaming
                # here to keep the connection obviously alive.
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        return self._client

    def _model_for(self, route: Route) -> str:
        return settings.groq_cheap_model if route == "cheap" else settings.groq_reasoning_model

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One chat completion, retrying the free tier's rate limit.

        429 is the expected steady state on a free key rather than an
        exceptional one, and Groq returns `retry-after` — so this honours the
        server's number instead of guessing a backoff.
        """
        client = self._http()
        last: httpx.HTTPStatusError | None = None

        for attempt in range(settings.groq_max_retries + 1):
            response = client.post("/chat/completions", json=payload)
            if response.status_code == 429 and attempt < settings.groq_max_retries:
                delay = float(response.headers.get("retry-after", 2 ** attempt))
                time.sleep(min(delay, 30.0))
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # Surface Groq's own message: "model not found" and "rate limit
                # exceeded" are the two an operator will actually hit, and the
                # bare status code names neither.
                detail = response.text[:400]
                raise LLMUnavailable(f"Groq {response.status_code}: {detail}") from exc
            return response.json()

        raise LLMUnavailable(f"Groq rate limit persisted after {settings.groq_max_retries} retries") from last

    @staticmethod
    def _messages(prompt: str, system: str | None) -> list[dict[str, str]]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        route: Route = "reasoning",
        max_tokens: int | None = None,
        effort: str | None = None,
        agent: str = "",
        prefill_json: bool = False,
    ) -> LLMResult:
        """One completion. Signature matches the Anthropic client exactly.

        `effort` and `prefill_json` are accepted and ignored — they are
        Anthropic-specific and the alternative is for every caller to know
        which provider is configured, which is the coupling this class exists
        to avoid.
        """
        model = self._model_for(route)
        max_tokens = max_tokens or settings.max_tokens

        payload: dict[str, Any] = {
            "model": model,
            "messages": self._messages(prompt, system),
            "max_tokens": max_tokens,
        }

        with tr.span(
            f"llm.{route}",
            **{
                tr.GEN_AI_SYSTEM: "groq",
                tr.GEN_AI_OPERATION: "chat",
                tr.GEN_AI_REQUEST_MODEL: model,
                tr.GEN_AI_REQUEST_MAX_TOKENS: max_tokens,
                tr.GEN_AI_AGENT_NAME: agent or route,
            },
        ) as sp:
            started = time.perf_counter()
            body = self._post(payload)
            latency_ms = int((time.perf_counter() - started) * 1000)
            return self._to_result(body, model, route, latency_ms, sp)

    def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
        route: Route = "cheap",
        agent: str = "",
        max_tokens: int = 2000,
    ) -> tuple[dict[str, Any], LLMResult]:
        """Structured extraction.

        Groq's JSON mode guarantees syntactically valid JSON but does not
        enforce a schema the way Anthropic's `json_schema` format does, so the
        schema is rendered into the system prompt as an instruction and the
        result still goes through `parse_json_loose`. That parser already
        existed as a fallback on the Anthropic path; here it is the primary
        defence, which is why the schema is stated rather than assumed.
        """
        model = self._model_for(route)

        # Groq rejects json_object mode unless the word "json" appears in the
        # conversation — an actual documented API constraint, not superstition.
        instruction = (
            f"{system}\n\n" if system else ""
        ) + f"Respond with a single JSON object matching this schema:\n{schema}"

        payload: dict[str, Any] = {
            "model": model,
            "messages": self._messages(prompt, instruction),
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        with tr.span(
            f"llm.{route}.json",
            **{
                tr.GEN_AI_SYSTEM: "groq",
                tr.GEN_AI_OPERATION: "chat",
                tr.GEN_AI_REQUEST_MODEL: model,
                tr.GEN_AI_AGENT_NAME: agent or route,
            },
        ) as sp:
            started = time.perf_counter()
            body = self._post(payload)
            latency_ms = int((time.perf_counter() - started) * 1000)
            result = self._to_result(body, model, route, latency_ms, sp)
            return parse_json_loose(result.text), result

    @staticmethod
    def _to_result(
        body: dict[str, Any], model: str, route: Route, latency_ms: int, sp: Any
    ) -> LLMResult:
        choices = body.get("choices") or [{}]
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        finish = choices[0].get("finish_reason") or ""

        usage = body.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        served = body.get("model") or model
        cost = price(served, in_tok, out_tok)

        sp.set_attribute(tr.GEN_AI_RESPONSE_MODEL, served)
        sp.set_attribute(tr.GEN_AI_USAGE_INPUT, in_tok)
        sp.set_attribute(tr.GEN_AI_USAGE_OUTPUT, out_tok)
        sp.set_attribute(tr.GEN_AI_RESPONSE_FINISH, [finish] if finish else [])
        sp.set_attribute(tr.AIOPS_COST_USD, round(cost, 6))

        return LLMResult(
            text=text,
            model=served,
            route=route,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            latency_ms=latency_ms,
            stop_reason=finish,
            # OpenAI-compatible APIs signal a content-policy stop with
            # finish_reason "content_filter"; there is no separate refusal
            # field, so this is the equivalent of Anthropic's refusal check.
            refused=finish == "content_filter",
        )
