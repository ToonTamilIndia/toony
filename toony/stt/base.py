"""Speech-to-text interface shared by the local and cloud backends."""

from __future__ import annotations

import abc
import io
import wave
from dataclasses import dataclass


class STTError(RuntimeError):
    pass


@dataclass
class Transcript:
    text: str
    language: str = ""
    duration_s: float = 0.0
    # Whisper reports how confident it is; used to reject noise.
    confidence: float = 1.0

    @property
    def usable(self) -> bool:
        stripped = self.text.strip(" .,!?\n\t")
        return bool(stripped) and len(stripped) > 1


class STT(abc.ABC):
    name = "stt"

    @abc.abstractmethod
    def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        """Turn 16-bit mono PCM into text."""

    def warm(self) -> None:
        """Preload models so the first real utterance is not slow."""

    def check(self) -> str:
        return "no check implemented"


def to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw PCM in a WAV container — what every cloud API expects."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()
