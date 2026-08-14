"""Tests for the Groq provider.

No key and no network: httpx's MockTransport stands in for Groq, which lets the
request shape be asserted directly. That is the part worth testing — the wire
format is the contract with a service this code cannot reach from CI, and every
bug in it looks identical from the outside (a 400 with a vendor message).
"""

from __future__ import annotations

import httpx
import pytest

from aiops.llm import LLMUnavailable, get_llm, reset_llm
from aiops.llm_groq import GroqClient


@pytest.fixture(autouse=True)
def clean_client():
    reset_llm()
    yield
    reset_llm()


def _completion(text: str = "hello", finish: str = "stop") -> dict:
    return {
        "model": "llama-3.3-70b-versatile",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def _client_with(handler) -> GroqClient:
    """A client wired to a fake transport, with the same headers `_http()` sets.

    The headers are duplicated here deliberately rather than left off: a helper
    that quietly drops authentication would let an auth bug pass every test.
    `test_http_client_sends_bearer_auth` asserts the real builder separately.
    """
    client = GroqClient(api_key="test-key")
    client._client = httpx.Client(
        base_url="https://api.groq.com/openai/v1",
        headers={"Authorization": "Bearer test-key"},
        transport=httpx.MockTransport(handler),
    )
    return client


def test_http_client_sends_bearer_auth(monkeypatch):
    """The real builder, not the test helper — this is what talks to Groq."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-real")
    built = GroqClient()._http()
    assert built.headers["authorization"] == "Bearer gsk-real"
    assert str(built.base_url).startswith("https://api.groq.com")


# --- provider selection ----------------------------------------------------


def test_default_provider_is_anthropic(monkeypatch):
    """The Groq work must not change what an unconfigured deployment does."""
    from aiops.config import settings
    from aiops.llm import LLMClient

    assert settings.llm_provider == "anthropic"
    assert isinstance(get_llm(), LLMClient)


def test_provider_groq_selects_the_groq_client(monkeypatch):
    from aiops.config import settings

    monkeypatch.setattr(settings, "llm_provider", "groq")
    assert isinstance(get_llm(), GroqClient)


def test_unknown_provider_is_rejected_rather_than_defaulted(monkeypatch):
    from aiops.config import settings

    monkeypatch.setattr(settings, "llm_provider", "openai")
    with pytest.raises(LLMUnavailable, match="expected 'anthropic' or 'groq'"):
        get_llm()


def test_missing_key_names_the_fix(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    client = GroqClient()
    assert client.available is False
    with pytest.raises(LLMUnavailable, match="console.groq.com"):
        client.complete("hi")


# --- request shape ---------------------------------------------------------


def test_complete_sends_the_openai_chat_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    result = _client_with(handler).complete("why did checkout fail?", system="Be terse.")

    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "why did checkout fail?"},
    ]
    assert result.text == "hello"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.stop_reason == "stop"
    assert result.refused is False


def test_anthropic_only_kwargs_are_accepted_and_ignored():
    """Callers must not have to know which provider is configured."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert "thinking" not in body
        assert "output_config" not in body
        return httpx.Response(200, json=_completion())

    result = _client_with(handler).complete("q", effort="high", prefill_json=True)
    assert result.text == "hello"


def test_routes_pick_different_models():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=_completion())

    client = _client_with(handler)
    client.complete("q", route="cheap")
    client.complete("q", route="reasoning")

    from aiops.config import settings

    assert seen == [settings.groq_cheap_model, settings.groq_reasoning_model]


def test_complete_json_requests_json_mode_and_states_the_schema():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(text='{"verdict": "answered"}'))

    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    parsed, result = _client_with(handler).complete_json("classify", schema)

    assert seen["body"]["response_format"] == {"type": "json_object"}
    # Groq rejects json_object mode unless "json" appears in the conversation.
    assert "json" in seen["body"]["messages"][0]["content"].lower()
    assert parsed == {"verdict": "answered"}
    assert result.output_tokens == 7


def test_complete_json_survives_a_fenced_response():
    """Groq guarantees valid JSON, not an unwrapped object — the parser matters."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(text='```json\n{"ok": true}\n```'))

    parsed, _ = _client_with(handler).complete_json("q", {"type": "object"})
    assert parsed == {"ok": True}


# --- failure handling ------------------------------------------------------


def test_rate_limit_is_retried_using_the_servers_retry_after():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        return httpx.Response(200, json=_completion())

    result = _client_with(handler).complete("q")
    assert calls["n"] == 2
    assert result.text == "hello"


def test_persistent_rate_limit_raises_rather_than_returning_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})

    with pytest.raises(LLMUnavailable):
        _client_with(handler).complete("q")


def test_api_error_surfaces_groqs_own_message():
    """A bare status code names neither a bad model id nor an expired key."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='{"error":{"message":"model not found"}}')

    with pytest.raises(LLMUnavailable, match="model not found"):
        _client_with(handler).complete("q")


def test_content_filter_is_reported_as_a_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(text="", finish="content_filter"))

    assert _client_with(handler).complete("q").refused is True


def test_free_tier_reports_zero_cost_rather_than_an_invented_rate():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion())

    assert _client_with(handler).complete("q").cost_usd == 0.0


# --- offline gate ----------------------------------------------------------


def test_groq_provider_ignores_a_stale_anthropic_key(monkeypatch):
    """A leftover ANTHROPIC_API_KEY must not make a keyless Groq deploy look online."""
    from aiops.config import settings
    from aiops.offline import is_offline

    monkeypatch.delenv("AIOPS_FORCE_OFFLINE", raising=False)
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leftover")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert is_offline() is True


def test_groq_key_brings_the_system_online(monkeypatch):
    from aiops.config import settings
    from aiops.offline import is_offline

    monkeypatch.delenv("AIOPS_FORCE_OFFLINE", raising=False)
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")

    assert is_offline() is False


def test_force_offline_still_wins(monkeypatch):
    from aiops.config import settings
    from aiops.offline import is_offline

    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("AIOPS_FORCE_OFFLINE", "1")

    assert is_offline() is True
