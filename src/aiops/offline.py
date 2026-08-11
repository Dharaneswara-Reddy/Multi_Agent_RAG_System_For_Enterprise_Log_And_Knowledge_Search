"""Deterministic offline LLM stand-in.

Purpose: the retrieval stack, the graph topology, the guardrails, and the audit
trail should all be testable without network access or credentials. CI runs this
mode, and so does anyone cloning the repo before they have an API key.

What it is honest about: this is **not** a model. It composes extractive answers
from the retrieved context with real citations, which is enough to exercise
every code path and to measure *retrieval* quality. It cannot measure answer
quality — `evaluation.run` reports faithfulness as `null` in offline mode rather
than reporting a number that means nothing.
"""

from __future__ import annotations

import re
from typing import Any

from aiops.llm import LLMResult

_REF_RE = re.compile(r"<source ref=\"([^\"]+)\"")
_ERROR_CODE_RE = re.compile(r"\b[A-Z]{2,6}-\d{3,5}\b")

_SERVICES = [
    "api-gateway",
    "auth-service",
    "order-service",
    "payment-service",
    "inventory-service",
    "notification-service",
    "search-service",
]

_LOG_WORDS = {"log", "logs", "happened", "when", "trace", "timeline", "error", "failed", "failing"}
_DOC_WORDS = {"how", "runbook", "procedure", "policy", "should", "why", "design", "decision"}


def _refs(context: str, limit: int = 4) -> list[str]:
    return _REF_RE.findall(context)[:limit]


def _sentences(text: str, limit: int) -> list[str]:
    body = re.sub(r"<[^>]+>", " ", text)
    body = re.sub(r"\s+", " ", body)
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if len(s.strip()) > 40]
    return parts[:limit]


class OfflineLLMClient:
    """Drop-in replacement for `LLMClient` with the same call surface."""

    available = True
    offline = True

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        route: str = "reasoning",
        max_tokens: int | None = None,
        effort: str | None = None,
        agent: str = "",
        prefill_json: bool = False,
    ) -> LLMResult:
        refs = _refs(prompt, 4)
        cited = " ".join(f"[{r}]" for r in refs)
        excerpt = _sentences(prompt, 3)

        if agent == "log_analyst":
            body = (
                "Log evidence (offline extractive mode). The retrieved windows show:\n"
                + "\n".join(f"- {s} {f'[{refs[i]}]' if i < len(refs) else ''}" for i, s in enumerate(excerpt))
                + f"\n\nSources consulted: {cited}"
            )
        elif agent == "knowledge":
            body = (
                "Documentation findings (offline extractive mode):\n"
                + "\n".join(f"- {s} {f'[{refs[i]}]' if i < len(refs) else ''}" for i, s in enumerate(excerpt))
                + f"\n\nSources consulted: {cited}"
            )
        else:
            body = (
                "**Offline mode — extractive summary, not a generated answer.**\n\n"
                "**What the evidence shows**\n"
                + "\n".join(f"- {s} {f'[{refs[i]}]' if i < len(refs) else ''}" for i, s in enumerate(excerpt))
                + "\n\n**Caveat**\nNo model was called. Set `ANTHROPIC_API_KEY` for "
                "synthesised answers, cause-vs-symptom reasoning, and faithfulness scoring.\n"
                + (f"\nSources: {cited}" if cited else "")
            )

        return LLMResult(
            text=body,
            model="offline-extractive",
            route="cheap",
            input_tokens=len(prompt) // 4,
            output_tokens=len(body) // 4,
            cost_usd=0.0,
            latency_ms=1,
            stop_reason="end_turn",
        )

    def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
        route: str = "cheap",
        agent: str = "",
        max_tokens: int = 2000,
    ) -> tuple[dict[str, Any], LLMResult]:
        """Rule-based triage. Same output shape as the model route."""
        # Strip the "Question: " wrapper the caller adds — leaving it in leaks a
        # high-frequency corpus word into the retrieval query and inflates the
        # top cosine, which then feeds a falsely confident answer.
        body = re.sub(r"^\s*question:\s*", "", prompt, flags=re.IGNORECASE).strip()
        q = body.lower()
        codes = sorted(set(_ERROR_CODE_RE.findall(body.upper())))
        services = [s for s in _SERVICES if s in q]

        wants_logs = bool(_LOG_WORDS & set(re.findall(r"[a-z]+", q)))
        wants_docs = bool(_DOC_WORDS & set(re.findall(r"[a-z]+", q)))
        if wants_logs and wants_docs:
            route_choice = "hybrid"
        elif wants_logs:
            route_choice = "logs"
        elif wants_docs:
            route_choice = "knowledge"
        else:
            route_choice = "hybrid"

        search_query = re.sub(r"\b(what|why|how|the|a|an|is|are|do|does|did|i|my)\b", " ", q)
        search_query = re.sub(r"\s+", " ", search_query).strip() or prompt

        data = {
            "route": route_choice,
            "error_codes": codes,
            "services": services,
            "search_query": search_query,
            "needs_human": bool(re.search(r"\b(delete|drop|truncate|restart|kill|refund)\b", q)),
            "reasoning": "offline rule-based routing",
        }
        result = LLMResult(
            text="",
            model="offline-rules",
            route="cheap",
            input_tokens=len(prompt) // 4,
            output_tokens=20,
            cost_usd=0.0,
            latency_ms=1,
            stop_reason="end_turn",
        )
        return data, result


def is_offline() -> bool:
    """True when no usable Anthropic credential is reachable.

    Constructing `anthropic.Anthropic()` is *not* a credential check — it
    succeeds with no key and only raises at request time. The SDK's resolution
    order is API key -> auth token -> an `ant auth login` profile on disk, so
    all three are checked here.
    """
    import os
    from pathlib import Path

    if os.getenv("AIOPS_FORCE_OFFLINE") == "1":
        return True
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return False

    config_dir = Path(os.getenv("ANTHROPIC_CONFIG_DIR", "~/.config/anthropic")).expanduser()
    creds = config_dir / "credentials"
    if creds.is_dir() and any(creds.glob("*.json")):
        return False

    # Workload Identity Federation is the remaining credential source.
    return not (
        os.getenv("ANTHROPIC_FEDERATION_RULE_ID")
        and (os.getenv("ANTHROPIC_IDENTITY_TOKEN_FILE") or os.getenv("ANTHROPIC_IDENTITY_TOKEN"))
    )


def resolve_llm():
    """Return the real client when credentials exist, the stub otherwise."""
    if is_offline():
        return OfflineLLMClient()
    from aiops.llm import get_llm

    return get_llm()
