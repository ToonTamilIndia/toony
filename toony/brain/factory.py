"""Pick a brain backend from configuration."""

from __future__ import annotations

from ..config import Config
from .base import Brain, BrainError

PROVIDERS = ("claude", "openai", "ollama")

# Models that can actually look at an image. A text-only model handed a
# screenshot does not say so — it answers confidently about nothing — which is
# why vision is routed rather than hoped for.
VISION_HINTS = ("claude", "gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4",
                "llava", "bakllava", "moondream", "minicpm-v", "llama3.2-vision",
                "qwen2-vl", "qwen2.5vl", "qwen3-vl", "gemma3", "pixtral",
                "internvl", "glm-4v", "vision", "-vl")

DEFAULT_VISION_MODEL = {
    "claude": "claude-opus-5",
    "openai": "gpt-4o-mini",
    "ollama": "qwen2.5vl:7b",
}


def can_see(provider: str, model: str) -> bool:
    """Whether this model accepts images, judged by name."""
    if provider == "claude":
        return True
    lowered = model.lower()
    return any(hint in lowered for hint in VISION_HINTS)


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


def build_vision(cfg: Config) -> Brain:
    """The model that gets shown screenshots.

    Usually the same brain. But the default local brain is a text-only 7B, and
    handing it a screenshot produces a confident description of nothing, so a
    separate vision model can be named and is used only for looking.
    """
    if not cfg.get("vision.enabled", True):
        raise BrainError("Looking at the screen is switched off (vision.enabled).")

    provider = str(cfg.get("vision.provider", "auto")).lower()
    brain_provider = str(cfg.get("brain.provider", "ollama")).lower()
    brain_model = str(cfg.get(f"brain.{brain_provider}.model", ""))

    if provider == "auto":
        if can_see(brain_provider, brain_model):
            return build_brain(cfg)
        provider = brain_provider
    elif provider == "brain":
        return build_brain(cfg)

    if provider not in PROVIDERS:
        raise BrainError(f"Unknown vision provider {provider!r}. "
                         f"Choose one of: auto, brain, {', '.join(PROVIDERS)}")

    model = (str(cfg.get("vision.model", "")).strip()
             or DEFAULT_VISION_MODEL.get(provider, ""))
    if provider == brain_provider and model == brain_model:
        return build_brain(cfg)

    if provider == "claude":
        from .claude import ClaudeBrain
        sec = cfg.section("brain.claude")
        return ClaudeBrain(model=model, api_key=cfg.api_key("brain.claude"),
                           max_tokens=int(sec["max_tokens"]), effort=sec["effort"],
                           thinking=sec["thinking"],
                           refusal_fallback=bool(sec["refusal_fallback"]))

    from .openai_compat import OpenAICompatBrain
    sec = cfg.section(f"brain.{provider}")
    return OpenAICompatBrain(model=model, base_url=sec["base_url"],
                             api_key=cfg.api_key(f"brain.{provider}"),
                             max_tokens=int(sec["max_tokens"]),
                             temperature=float(cfg.get("brain.temperature", 0.5)),
                             name=f"{provider}-vision")


def vision_summary(cfg: Config) -> str:
    """One line for `toony doctor`, without building anything."""
    if not cfg.get("vision.enabled", True):
        return "off"
    provider = str(cfg.get("vision.provider", "auto")).lower()
    brain_provider = str(cfg.get("brain.provider", "ollama")).lower()
    brain_model = str(cfg.get(f"brain.{brain_provider}.model", ""))
    if provider in ("auto", "brain") and can_see(brain_provider, brain_model):
        return f"{brain_provider}:{brain_model} (the brain can see)"
    if provider == "brain":
        return f"{brain_provider}:{brain_model} — this model cannot read images"
    target = brain_provider if provider == "auto" else provider
    model = (str(cfg.get("vision.model", "")).strip()
             or DEFAULT_VISION_MODEL.get(target, "?"))
    note = "" if can_see(target, model) else " — this model cannot read images"
    return f"{target}:{model}{note}"
