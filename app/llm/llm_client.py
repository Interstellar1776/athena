#!/usr/bin/env python3
"""llm_client.py — the provider-agnostic LLM doorway.

Build Sequence §19 step 4 (context doc §16). One call surface — ``call_llm(system, user)`` — over
three thin provider adapters, so the rest of Athena never knows or cares which model answered. The
provider/model/endpoint/key are **environment config** (§16), never hardcoded; selecting a provider is
one env var, and adding a new provider is one more adapter + one more ``LLM_PROVIDER`` value.

    LLM_PROVIDER   ollama (default) | anthropic | openai
    LLM_MODEL      the model name (required) — e.g. qwen3:32b, or a Claude/GPT model id
    LLM_ENDPOINT   base-URL override (mainly local Ollama); optional for the cloud SDKs
    LLM_API_KEY    required for cloud providers; unused by local Ollama

The call patterns are *nearly* identical but differ in shape (Anthropic Messages API vs OpenAI vs
Ollama's native API), so each provider gets a small adapter behind the shared interface. SDKs are
imported **lazily inside each adapter**, so importing this module never requires a provider you aren't
using. Failures halt loudly with provider context (§17).

CLI (smoke test):
    LLM_PROVIDER=ollama LLM_MODEL=qwen3:32b python -m app.llm.llm_client "Say hello in one short line."
"""

from __future__ import annotations

import os

DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"
DEFAULT_ANTHROPIC_MAX_TOKENS = 1024


class LLMError(RuntimeError):
    """An LLM request failed — carries the provider for a loud, actionable halt (§17)."""


# ===========================================================================
# 1. Public surface — resolve config, dispatch to the provider adapter
# ===========================================================================
def call_llm(system: str, user: str, *, model: str | None = None, max_tokens: int = 1024) -> str:
    """Send a (system, user) prompt to the configured provider; return the model's text.

    ``model=None`` falls back to ``LLM_MODEL`` (the env default); passing ``model`` overrides it per
    call — the seam a future UI model-picker writes straight through.
    """
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    resolved_model = model or os.getenv("LLM_MODEL")
    if not resolved_model:
        raise LLMError("LLM_MODEL is not set and no model was passed — set the env var or pass model=.")

    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise LLMError(f"unknown LLM_PROVIDER {provider!r} — expected one of {sorted(_ADAPTERS)}.")

    try:
        return adapter(system, user, resolved_model, max_tokens)
    except LLMError:
        raise
    except Exception as exc:                                  # noqa: BLE001 — fail loud, with provider context
        raise LLMError(f"call_llm: {provider} request failed: {exc}") from exc


# ===========================================================================
# 2. Provider adapters — one per backend, SDK imported lazily inside
# ===========================================================================
def _ollama(system: str, user: str, model: str, max_tokens: int) -> str:
    """Local Ollama via its native /api/chat (no API key; runs on the user's machine)."""
    import httpx

    endpoint = os.getenv("LLM_ENDPOINT") or DEFAULT_OLLAMA_ENDPOINT
    resp = httpx.post(
        f"{endpoint.rstrip('/')}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "options": {"num_predict": max_tokens},
        },
        timeout=300.0,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def _anthropic(system: str, user: str, model: str, max_tokens: int) -> str:
    """Anthropic Claude via the official SDK. Adaptive thinking; no temperature (removed on Opus 4.8)."""
    from anthropic import Anthropic

    kwargs = {"api_key": os.getenv("LLM_API_KEY")} if os.getenv("LLM_API_KEY") else {}
    if os.getenv("LLM_ENDPOINT"):
        kwargs["base_url"] = os.getenv("LLM_ENDPOINT")
    client = Anthropic(**kwargs)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
    )
    # Content is a list of blocks; keep only the text blocks (skip any thinking blocks).
    return "".join(block.text for block in msg.content if getattr(block, "type", None) == "text")


def _openai(system: str, user: str, model: str, max_tokens: int) -> str:
    """OpenAI GPT via the official SDK (chat completions)."""
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("LLM_API_KEY"), base_url=os.getenv("LLM_ENDPOINT") or None)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content or ""


_ADAPTERS = {"ollama": _ollama, "anthropic": _anthropic, "openai": _openai}


# ===========================================================================
# 3. CLI — a one-shot smoke test against the configured provider
# ===========================================================================
def main() -> int:
    import sys

    prompt = " ".join(sys.argv[1:]) or "Say hello in one short line."
    print(call_llm("You are a terse assistant.", prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
