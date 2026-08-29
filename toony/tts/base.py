"""Text-to-speech interface shared by the Piper, cloud and espeak backends."""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass


class TTSError(RuntimeError):
    pass


@dataclass
class Speech:
    pcm: bytes
    sample_rate: int
    channels: int = 1


class TTS(abc.ABC):
    name = "tts"

    @abc.abstractmethod
    def synthesise(self, text: str) -> Speech:
        """Render text to 16-bit PCM."""

    def warm(self) -> None:
        """Preload the voice so the first reply is not delayed."""

    def check(self) -> str:
        return "no check implemented"


_ABBREVIATIONS = ("mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "e.g.", "i.e.",
                  "etc.", "vs.", "no.", "fig.")


def sentences(text: str):
    """Split into speakable chunks so playback can start before the model finishes.

    Yields whole sentences; anything longer than a breath is split on commas.
    """
    text = clean_for_speech(text)
    buffer = ""
    for piece in re.split(r"(?<=[.!?])\s+", text):
        candidate = (buffer + " " + piece).strip() if buffer else piece
        # Do not break on "Dr." and friends — wait for the next fragment.
        if candidate.lower().endswith(_ABBREVIATIONS):
            buffer = candidate
            continue
        buffer = ""
        for chunk in _split_long(candidate):
            if chunk.strip():
                yield chunk.strip()
    if buffer.strip():
        yield buffer.strip()


def _split_long(sentence: str, limit: int = 240):
    if len(sentence) <= limit:
        yield sentence
        return
    current = ""
    for part in re.split(r"(?<=,)\s+", sentence):
        if current and len(current) + len(part) > limit:
            yield current
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        yield current


_MARKDOWN = [
    (re.compile(r"```.*?```", re.S), " code block "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)"), r"\1"),
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),
    (re.compile(r"^\s*#{1,6}\s*", re.M), ""),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
    (re.compile(r"https?://\S+"), " a link "),
    (re.compile(r"[ \t]+"), " "),
]


def clean_for_speech(text: str) -> str:
    """Strip anything a synthesiser would read out as punctuation soup."""
    out = text.strip()
    for pattern, replacement in _MARKDOWN:
        out = pattern.sub(replacement, out)
    # Emoji and symbols that have no spoken form.
    out = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", out)
    return out.strip()
