"""Pick a brain backend from configuration."""

from __future__ import annotations

from ..config import Config
from .base import Brain, BrainError

PROVIDERS = ("claude", "openai", "ollama")


def build_brain(cfg: Config) -> Brain:
    provider = str(cfg.get("brain.provider", "ollama")).lower()
    if provider not in PROVIDERS:
        raise BrainError(f"Unknown brain provider {provider!r}. "
                         f"Choose one of: {', '.join(PROVIDERS)}")

    if provider == "claude":
        from .claude import ClaudeBrain
        sec = cfg.section("brain.claude")
        return ClaudeBrain(
            model=sec["model"],
            api_key=cfg.api_key("brain.claude"),
            max_tokens=int(sec["max_tokens"]),
            effort=sec["effort"],
            thinking=sec["thinking"],
            refusal_fallback=bool(sec["refusal_fallback"]),
        )

    from .openai_compat import OpenAICompatBrain
    sec = cfg.section(f"brain.{provider}")
    return OpenAICompatBrain(
        model=sec["model"],
        base_url=sec["base_url"],
        api_key=cfg.api_key(f"brain.{provider}"),
        max_tokens=int(sec["max_tokens"]),
        temperature=float(cfg.get("brain.temperature", 0.5)),
        name=provider,
    )
