"""Cloud text-to-speech over the OpenAI-compatible speech endpoint."""

from __future__ import annotations

import io
import wave

from ..log import get
from .base import TTS, Speech, TTSError

log = get("tts.cloud")


class CloudTTS(TTS):
    name = "openai"

    def __init__(self, model: str, voice: str, base_url: str, api_key: str,
                 speed: float = 1.0):
        try:
            import openai
        except ImportError as exc:
            raise TTSError("Cloud speech needs the openai package: "
                           "pip install 'toony[openai]'") from exc
        self._openai = openai
        self.client = openai.OpenAI(base_url=base_url or None,
                                    api_key=api_key or "not-needed", timeout=30.0)
        self.model = model
        self.voice = voice
        self.speed = float(speed)

    def synthesise(self, text: str) -> Speech:
        if not text.strip():
            return Speech(b"", 24000)
        try:
            response = self.client.audio.speech.create(
                model=self.model, voice=self.voice, input=text,
                response_format="wav", speed=self.speed)
            data = response.read()
        except self._openai.APIConnectionError as exc:
            raise TTSError("I could not reach the speech service.") from exc
        except self._openai.APIStatusError as exc:
            raise TTSError(f"Speech synthesis failed: {exc}") from exc
        with wave.open(io.BytesIO(data), "rb") as wav:
            return Speech(pcm=wav.readframes(wav.getnframes()),
                          sample_rate=wav.getframerate(),
                          channels=wav.getnchannels())

    def check(self) -> str:
        try:
            self.client.models.list()
            return f"reachable, voice {self.voice} on {self.model}"
        except Exception as exc:
            return f"unreachable: {exc.__class__.__name__}"
