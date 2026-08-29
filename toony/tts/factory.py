"""Pick a text-to-speech backend from configuration."""

from __future__ import annotations

from .base import TTS, TTSError

PROVIDERS = ("piper", "openai", "espeak")


def build_tts(cfg) -> TTS:
    provider = str(cfg.get("tts.provider", "piper")).lower()
    speed = float(cfg.get("tts.speed", 1.0))

    if provider == "piper":
        from .piper import PiperTTS
        sec = cfg.section("tts.piper")
        return PiperTTS(voice=sec["voice"], model_dir=sec["model_dir"],
                        binary=sec["binary"], speed=speed)
    if provider == "openai":
        from .cloud import CloudTTS
        sec = cfg.section("tts.openai")
        return CloudTTS(model=sec["model"], voice=sec["voice"],
                        base_url=sec["base_url"],
                        api_key=cfg.api_key("tts.openai"), speed=speed)
    if provider == "espeak":
        from .espeak import EspeakTTS
        sec = cfg.section("tts.espeak")
        return EspeakTTS(voice=sec["voice"],
                         words_per_minute=int(sec["words_per_minute"]), speed=speed)
    raise TTSError(f"Unknown speech provider {provider!r}. "
                   f"Choose one of: {', '.join(PROVIDERS)}")
