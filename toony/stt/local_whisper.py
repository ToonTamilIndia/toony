"""Local speech recognition with faster-whisper (CTranslate2)."""

from __future__ import annotations

import time

from ..log import get
from .base import STT, STTError, Transcript

log = get("stt.local")

# Rough VRAM cost on GPU. Used only for the advice in `toony doctor`.
MODEL_SIZES = {"tiny": 0.4, "base": 0.6, "small": 1.2, "medium": 2.6,
               "large-v3": 4.7, "distil-large-v3": 2.5}


class LocalWhisper(STT):
    name = "local"

    def __init__(self, model: str = "small", device: str = "auto",
                 compute_type: str = "auto", beam_size: int = 1,
                 language: str = "en", initial_prompt: str = ""):
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.beam_size = max(1, int(beam_size))
        self.language = language or None
        self.initial_prompt = initial_prompt or None
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTError(
                "Local speech recognition needs faster-whisper: "
                "pip install 'toony[local]'"
            ) from exc

        device, compute = self._pick_device()
        log.info("loading whisper %s on %s (%s)", self.model_name, device, compute)
        started = time.monotonic()
        try:
            self._model = WhisperModel(self.model_name, device=device,
                                       compute_type=compute)
        except Exception as exc:
            if device == "cuda":
                # A missing cuDNN or too little VRAM should not be fatal.
                log.warning("CUDA load failed (%s) — falling back to CPU", exc)
                self.device, self.compute_type = "cpu", "int8"
                self._model = WhisperModel(self.model_name, device="cpu",
                                           compute_type="int8")
            else:
                raise STTError(f"could not load whisper {self.model_name}: {exc}") from exc
        log.info("whisper ready in %.1fs", time.monotonic() - started)
        return self._model

    def _pick_device(self) -> tuple[str, str]:
        device = self.device
        if device == "auto":
            device = "cuda" if _has_cuda() else "cpu"
        compute = self.compute_type
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        return device, compute

    def warm(self) -> None:
        self._load()

    def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        import numpy as np

        if not pcm:
            return Transcript("")
        model = self._load()
        audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        if sample_rate != 16000:
            audio = _resample(audio, sample_rate, 16000)

        started = time.monotonic()
        segments, info = model.transcribe(
            audio, language=self.language, beam_size=self.beam_size,
            initial_prompt=self.initial_prompt, vad_filter=True,
            condition_on_previous_text=False)
        parts, probabilities = [], []
        for segment in segments:
            parts.append(segment.text)
            probabilities.append(getattr(segment, "avg_logprob", 0.0))
        text = " ".join(p.strip() for p in parts).strip()
        log.info("transcribed %.1fs of audio in %.2fs: %r",
                 len(audio) / 16000, time.monotonic() - started, text[:80])
        confidence = 1.0
        if probabilities:
            # avg_logprob below about -1.0 is usually noise, not words.
            mean = sum(probabilities) / len(probabilities)
            confidence = max(0.0, min(1.0, 1.0 + mean))
        return Transcript(text=text, language=getattr(info, "language", "") or "",
                          duration_s=len(audio) / 16000, confidence=confidence)

    def check(self) -> str:
        device, compute = self._pick_device()
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return "faster-whisper is not installed"
        return f"faster-whisper ready, model {self.model_name} on {device} ({compute})"


def _has_cuda() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _resample(audio, source_rate: int, target_rate: int):
    """Linear resample. Capture is configured at 16 kHz, so this rarely runs."""
    import numpy as np

    if source_rate == target_rate:
        return audio
    count = int(round(len(audio) * target_rate / source_rate))
    return np.interp(np.linspace(0, len(audio), count, endpoint=False),
                     np.arange(len(audio)), audio).astype("float32")
