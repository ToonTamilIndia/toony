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

    Nothing is rewritten here. Normalising a URL after splitting on full stops
    is too late — the URL is already three fragments — so :func:`toony.text.
    speakable` runs on each whole sentence, just before it is synthesised.
    """
    text = _presplit(text)
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


# A code block must go before the text is split into sentences: its contents
# are full of full stops, and each one would otherwise become its own utterance.
_CODE_FENCE = re.compile(r"```.*?```", re.S)


def _presplit(text: str) -> str:
    """The one rewrite that has to happen before sentences are found."""
    return _CODE_FENCE.sub(" I have put the code on screen. ", text).strip()


def clean_for_speech(text: str) -> str:
    """Strip anything a synthesiser would read out as punctuation soup.

    Kept as the single entry point; the work lives in :mod:`toony.text`, which
    also names file paths and links instead of spelling them out.
    """
    from ..text import speakable

    return speakable(text)
