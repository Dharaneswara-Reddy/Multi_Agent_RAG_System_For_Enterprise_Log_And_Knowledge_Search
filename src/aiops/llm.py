"""Anthropic client wrapper: routing, cost accounting, structured output, tracing.

Two things this layer exists to make explicit, because both are JD line items
and neither is visible if you call the SDK directly from agent code:

- **Cost/latency routing.** Cheap, high-volume, mechanical work (classification,
  structured extraction) goes to Haiku. Synthesis and root-cause reasoning go to
  Opus. Every call records which route it took and what it cost, so the
  trade-off is measured rather than asserted.
- **Every call is a GenAI span.** Token counts and cost land on the span using
  the OTel GenAI attribute names, so the dashboard and any external collector
  read the same data.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic

from aiops.config import settings
from aiops.observability import tracing as tr

Route = Literal["cheap", "reasoning"]

# USD per 1M tokens. Kept here rather than fetched so cost math is deterministic
# in tests; refresh when pricing changes.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = PRICING.get(model, PRICING["claude-opus-5"])
    return (input_tokens * inp + output_tokens * out) / 1_000_000


@dataclass
class LLMResult:
    text: str
    model: str
    route: Route
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    stop_reason: str = ""
    refused: bool = False

    def as_step_fields(self) -> dict[str, Any]:
        return {
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
        }


@dataclass
class UsageLedger:
    """Accumulates spend across a single request so the UI can show a per-answer cost."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    by_route: dict[str, float] = field(default_factory=dict)

    def add(self, r: LLMResult) -> None:
        self.calls += 1
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.cost_usd += r.cost_usd
        self.by_route[r.route] = self.by_route.get(r.route, 0.0) + r.cost_usd


class LLMUnavailable(RuntimeError):
    """Raised when no API credentials are configured."""


class LLMClient:
    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._client: anthropic.Anthropic | None = None

    @property
    def available(self) -> bool:
        # An unset ANTHROPIC_API_KEY does not prove there are no credentials —
        # the SDK also resolves an `ant auth login` profile — so construct the
        # client and let it decide.
        if self._key:
            return True
        try:
            anthropic.Anthropic()
            return True
        except Exception:
            return False

    def _get(self) -> anthropic.Anthropic:
        if self._client is None:
            try:
                self._client = anthropic.Anthropic(api_key=self._key) if self._key else anthropic.Anthropic()
            except Exception as exc:  # pragma: no cover - depends on environment
                raise LLMUnavailable(str(exc)) from exc
        return self._client

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
        """One completion. Errors are surfaced, never silently swallowed."""
        model = settings.cheap_model if route == "cheap" else settings.reasoning_model
        max_tokens = max_tokens or settings.max_tokens

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        # Haiku 4.5 predates the effort/adaptive-thinking parameters; only the
        # frontier models accept them.
        if route == "reasoning":
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": effort or settings.effort}

        with tr.span(
            f"llm.{route}",
            **{
                tr.GEN_AI_SYSTEM: "anthropic",
                tr.GEN_AI_OPERATION: "chat",
                tr.GEN_AI_REQUEST_MODEL: model,
                tr.GEN_AI_REQUEST_MAX_TOKENS: max_tokens,
                tr.GEN_AI_AGENT_NAME: agent or route,
            },
        ) as sp:
            started = time.perf_counter()
            client = self._get()
            # Streaming avoids HTTP timeouts on long reasoning turns and costs
            # nothing when the response is short.
            with client.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
            latency_ms = int((time.perf_counter() - started) * 1000)

            text = "".join(b.text for b in message.content if b.type == "text")
            usage = message.usage
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            cost = price(model, in_tok, out_tok)
            stop_reason = message.stop_reason or ""

            sp.set_attribute(tr.GEN_AI_RESPONSE_MODEL, message.model or model)
            sp.set_attribute(tr.GEN_AI_USAGE_INPUT, in_tok)
            sp.set_attribute(tr.GEN_AI_USAGE_OUTPUT, out_tok)
            sp.set_attribute(tr.GEN_AI_RESPONSE_FINISH, [stop_reason] if stop_reason else [])
            sp.set_attribute(tr.AIOPS_COST_USD, round(cost, 6))

            return LLMResult(
                text=text,
                model=message.model or model,
                route=route,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                latency_ms=latency_ms,
                stop_reason=stop_reason,
                # A refusal is a normal 200 response, not an exception — callers
                # must check rather than assume `content` is populated.
                refused=stop_reason == "refusal",
            )

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
        """Structured extraction with a JSON-schema constraint.

        Falls back to brace-matching if the model wraps the object in prose,
        which structured outputs should prevent but older routes may not.
        """
        model = settings.cheap_model if route == "cheap" else settings.reasoning_model
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if system:
            kwargs["system"] = system

        with tr.span(
            f"llm.{route}.json",
            **{
                tr.GEN_AI_SYSTEM: "anthropic",
                tr.GEN_AI_OPERATION: "chat",
                tr.GEN_AI_REQUEST_MODEL: model,
                tr.GEN_AI_AGENT_NAME: agent or route,
            },
        ) as sp:
            started = time.perf_counter()
            message = self._get().messages.create(**kwargs)
            latency_ms = int((time.perf_counter() - started) * 1000)

            text = "".join(b.text for b in message.content if b.type == "text")
            usage = message.usage
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            cost = price(model, in_tok, out_tok)
            sp.set_attribute(tr.GEN_AI_USAGE_INPUT, in_tok)
            sp.set_attribute(tr.GEN_AI_USAGE_OUTPUT, out_tok)
            sp.set_attribute(tr.AIOPS_COST_USD, round(cost, 6))

            result = LLMResult(
                text=text,
                model=message.model or model,
                route=route,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                latency_ms=latency_ms,
                stop_reason=message.stop_reason or "",
                refused=message.stop_reason == "refusal",
            )
            return parse_json_loose(text), result


def parse_json_loose(text: str) -> dict[str, Any]:
    """Parse JSON that may be fenced or surrounded by prose. Returns {} on failure."""
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return {}


_CLIENT: LLMClient | None = None


def get_llm() -> LLMClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = LLMClient()
    return _CLIENT
