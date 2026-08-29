"""Cloud speech recognition over the OpenAI-compatible audio endpoint."""

from __future__ import annotations

import io

from ..log import get
from .base import STT, STTError, Transcript, to_wav

log = get("stt.cloud")


class CloudWhisper(STT):
    name = "openai"

    def __init__(self, model: str, base_url: str, api_key: str,
                 language: str = "en", initial_prompt: str = ""):
        try:
            import openai
        except ImportError as exc:
            raise STTError("Cloud speech recognition needs the openai package: "
                           "pip install 'toony[openai]'") from exc
        self._openai = openai
        self.client = openai.OpenAI(base_url=base_url or None,
                                    api_key=api_key or "not-needed", timeout=30.0)
        self.model = model
        self.language = language or None
        self.initial_prompt = initial_prompt or None

    def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        if not pcm:
            return Transcript("")
        payload = io.BytesIO(to_wav(pcm, sample_rate))
        payload.name = "utterance.wav"  # the API infers the format from the name
        try:
            response = self.client.audio.transcriptions.create(
                model=self.model, file=payload, language=self.language,
                prompt=self.initial_prompt)
        except self._openai.APIConnectionError as exc:
            raise STTError("I could not reach the transcription service.") from exc
        except self._openai.APIStatusError as exc:
            raise STTError(f"Transcription failed: {exc}") from exc
        text = getattr(response, "text", "") or ""
        return Transcript(text=text.strip(), duration_s=len(pcm) / 2 / sample_rate)

    def check(self) -> str:
        try:
            self.client.models.list()
            return f"reachable, using {self.model}"
        except Exception as exc:
            return f"unreachable: {exc.__class__.__name__}"
