"""Local speech recognition with faster-whisper (CTranslate2)."""

from __future__ import annotations

import time

from ..log import get
from .base import STT, STTError, Transcript

log = get("stt.local")

# Rough VRAM cost on GPU. Used only for the advice in `toony doctor`.
MODEL_SIZES = {"tiny": 0.4, "base": 0.6, "small": 1.2, "medium": 2.6,
               "large-v3": 4.7, "distil-large-v3": 2.5}

# CTranslate2 loads CUDA lazily, so a model can build on the GPU and then fail
# on the first encode. `cuda.preload()` loads the libraries pip put inside
# site-packages, where the linker does not look, so LD_LIBRARY_PATH is not
# needed — and cannot be lost when the systemd unit is rewritten.


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

    # Whisper on a GPU is roughly twenty times faster than on a CPU, so the
    # model that gives a half-second transcription on a 3050 gives ten seconds
    # without it. "auto" therefore means a different size on each.
    AUTO_MODEL = {"cuda": "small", "cpu": "base.en"}

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
        self.model_name = self._pick_model(device)
        if device == "cuda":
            from . import cuda

            cuda.preload()
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
                self.model_name = self._pick_model("cpu")
                self._model = WhisperModel(self.model_name, device="cpu",
                                           compute_type="int8")
            else:
                raise STTError(f"could not load whisper {self.model_name}: {exc}") from exc
        log.info("whisper ready in %.1fs", time.monotonic() - started)
        return self._model

    def _pick_model(self, device: str) -> str:
        """Which size to load. "auto" fits the size to what will run it."""
        if self.model_name != "auto":
            return self.model_name
        chosen = self.AUTO_MODEL.get(device, "base.en")
        if device != "cuda":
            log.info("no usable GPU — using whisper %s, which is about five "
                     "times faster on a CPU than 'small' (set stt.local.model "
                     "to override)", chosen)
        return chosen

    def _pick_device(self) -> tuple[str, str]:
        from . import cuda

        device = self.device
        if device == "auto":
            device = "cuda" if (_has_cuda() and cuda.usable()) else "cpu"
        compute = self.compute_type
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        return device, compute

    def _fall_back_to_cpu(self, reason: str) -> None:
        """Rebuild on the CPU. Slower is better than a turn that vanishes."""
        from faster_whisper import WhisperModel

        from . import cuda

        log.error("GPU transcription failed: %s", reason)
        log.error("%s", cuda.advice() or "falling back to the CPU")
        self.device, self.compute_type = "cpu", "int8"
        self.model_name = self._pick_model("cpu")
        self._model = WhisperModel(self.model_name, device="cpu",
                                   compute_type="int8")
        log.info("whisper is now running on the CPU")

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
        try:
            parts, probabilities, info = self._run(model, audio)
        except RuntimeError as exc:
            # CTranslate2 only touches cuBLAS on the first encode, so a broken
            # CUDA install surfaces here rather than at load time.
            if self.device != "cuda" or not _is_missing_library(exc):
                raise STTError(f"transcription failed: {exc}") from exc
            self._fall_back_to_cpu(str(exc))
            parts, probabilities, info = self._run(self._model, audio)
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

    def _run(self, model, audio):
        segments, info = model.transcribe(
            audio, language=self.language, beam_size=self.beam_size,
            initial_prompt=self.initial_prompt, vad_filter=True,
            condition_on_previous_text=False)
        parts, probabilities = [], []
        for segment in segments:          # lazy: this is where CUDA actually runs
            parts.append(segment.text)
            probabilities.append(getattr(segment, "avg_logprob", 0.0))
        return parts, probabilities, info

    def check(self) -> str:
        device, compute = self._pick_device()
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return "faster-whisper is not installed"
        from . import cuda

        note = ""
        if device == "cuda":
            absent = cuda.missing()
            if absent:
                note = f" — but {', '.join(absent)} cannot be loaded"
        elif self.device == "cuda":
            note = " (asked for cuda, using the CPU)"
        return (f"faster-whisper ready, model {self.model_name} "
                f"on {device} ({compute}){note}")


def _is_missing_library(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "not found or cannot be loaded" in text or "libcu" in text


def missing_cuda_libraries() -> list[str]:
    """Which CUDA libraries CTranslate2 will fail on. Empty means it will work."""
    from . import cuda

    return cuda.missing()


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
