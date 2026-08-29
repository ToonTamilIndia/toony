"""Pick a speech-to-text backend from configuration."""

from __future__ import annotations

from .base import STT, STTError

PROVIDERS = ("local", "openai")


def build_stt(cfg) -> STT:
    provider = str(cfg.get("stt.provider", "local")).lower()
    language = str(cfg.get("general.language", "en"))
    initial_prompt = str(cfg.get("stt.initial_prompt", "") or "")

    if provider == "local":
        from .local_whisper import LocalWhisper
        sec = cfg.section("stt.local")
        return LocalWhisper(model=sec["model"], device=sec["device"],
                            compute_type=sec["compute_type"],
                            beam_size=int(sec["beam_size"]),
                            language=language, initial_prompt=initial_prompt)
    if provider == "openai":
        from .cloud_whisper import CloudWhisper
        sec = cfg.section("stt.openai")
        return CloudWhisper(model=sec["model"], base_url=sec["base_url"],
                            api_key=cfg.api_key("stt.openai"),
                            language=language, initial_prompt=initial_prompt)
    raise STTError(f"Unknown speech-to-text provider {provider!r}. "
                   f"Choose one of: {', '.join(PROVIDERS)}")
