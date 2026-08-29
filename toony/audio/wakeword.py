"""Wake-word listening, with two engines behind one microphone loop.

openWakeWord is accurate and nearly free to run, but it only knows the phrases
somebody has trained a model for — and "hey Toony" is not one of them. So there
is a second engine that runs a tiny Whisper over short bursts of speech and
matches the phrase you actually want. It costs more CPU and fires a little less
reliably, but it works today, for any phrase, with nothing to train.

Either way the stream is released while Toony is recording or speaking, so the
assistant never hears itself.
"""

from __future__ import annotations

import difflib
import inspect
import re
import threading
import time
from pathlib import Path
from typing import Callable

from ..log import get

log = get("audio.wakeword")

CHUNK = 1280            # openWakeWord expects 80 ms of 16 kHz audio per call
RATE = 16000

_BUNDLED = {"hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy", "timer",
            "weather"}


def suggest_engine(phrase: str) -> str:
    """Which engine can actually hear this phrase."""
    slug = re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")
    return "openwakeword" if slug in _BUNDLED else "whisper"


# ---------------------------------------------------------------- detectors
class Detector:
    """Fed 80 ms of PCM at a time; returns a phrase name when it hears one."""

    name = "detector"

    def feed(self, pcm: bytes) -> str | None:
        raise NotImplementedError

    def reset(self) -> None:
        pass

    def check(self) -> str:
        return "ready"


def _openwakeword_paths(name: str, model_dir: Path) -> list[str]:
    """Turn a model name into real file paths.

    openWakeWord 0.4 takes ``wakeword_model_paths`` and will not look a bundled
    name up for you; 0.5 renamed the argument to ``wakeword_models`` and accepts
    either. Resolving to a path first works on both.
    """
    custom = model_dir / f"{name}.onnx"
    if custom.is_file():
        return [str(custom)]
    for suffix in (".tflite", ".onnx"):
        candidate = model_dir / f"{name}{suffix}"
        if candidate.is_file():
            return [str(candidate)]
    if Path(name).is_file():
        return [name]

    try:
        import openwakeword
    except ImportError:
        return []

    # The bundled models are indexed in openwakeword.MODELS on every version
    # that has them; the key is sometimes the pretty name, sometimes the slug.
    registry = getattr(openwakeword, "MODELS", {}) or {}
    for key, entry in registry.items():
        slug = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
        if slug != name.lower() and str(key) != name:
            continue
        path = entry.get("model_path") if isinstance(entry, dict) else entry
        if path and Path(str(path)).is_file():
            return [str(path)]

    # Last resort: look through the package's own resources.
    package = Path(getattr(openwakeword, "__file__", "") or "").parent
    for base in (package / "resources" / "models", package / "models"):
        if not base.is_dir():
            continue
        for suffix in ("onnx", "tflite"):
            hits = sorted(base.glob(f"{name}*.{suffix}"))
            hits = [h for h in hits if "melspectrogram" not in h.name
                    and "embedding" not in h.name]
            if hits:
                return [str(hits[0])]
    return []


class OpenWakeWordDetector(Detector):
    name = "openwakeword"

    def __init__(self, config):
        self.threshold = float(config.get("wakeword.threshold", 0.5))
        self.model = self._load(config)

    def _load(self, config):
        # openWakeWord asks onnxruntime for CUDA whether or not it is there and
        # prints a warning when it is not. It runs fine on the CPU — this is an
        # 80 ms model — so the noise is not worth alarming anybody with.
        import warnings

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*CUDAExecutionProvider.*")
            warnings.filterwarnings("ignore", category=UserWarning,
                                    module="onnxruntime.*")
            try:
                from openwakeword.model import Model
            except ImportError as exc:
                raise RuntimeError("Wake word needs openwakeword: "
                                   "pip install 'toony[wake]'") from exc

            name = str(config.get("wakeword.model", "hey_jarvis"))
            directory = Path(str(config.get("wakeword.model_dir", ""))).expanduser()
            paths = _openwakeword_paths(name, directory)
            if not paths:
                raise RuntimeError(
                    f"openWakeWord has no model called {name!r}, and there is no "
                    f"{name}.onnx in {directory}. Either download one "
                    f"(python -m openwakeword.utils.download_models), or switch "
                    f"to the whisper engine, which matches any phrase: "
                    f"toony wakeword \"hey toony\"")

            log.info("loading wake word model %s", paths[0])
            try:
                return Model(**self._argument(Model, paths),
                             **self._framework(Model, paths[0]))
            except Exception as exc:
                raise RuntimeError(
                    f"could not load wake word model {paths[0]}: {exc}") from exc

    @staticmethod
    def _argument(model_class, paths: list[str]) -> dict:
        """0.4 calls it wakeword_model_paths; 0.5 calls it wakeword_models."""
        try:
            parameters = inspect.signature(model_class.__init__).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "wakeword_models" in parameters:
            return {"wakeword_models": paths}
        if "wakeword_model_paths" in parameters:
            return {"wakeword_model_paths": paths}
        return {"wakeword_models": paths}

    @staticmethod
    def _framework(model_class, path: str) -> dict:
        """Match the runtime to the file, or a .onnx model loads as tflite."""
        try:
            parameters = inspect.signature(model_class.__init__).parameters
        except (TypeError, ValueError):
            return {}
        if "inference_framework" not in parameters:
            return {}
        return {"inference_framework": "onnx" if path.endswith(".onnx") else "tflite"}

    def feed(self, pcm: bytes) -> str | None:
        import numpy as np

        scores = self.model.predict(np.frombuffer(pcm, dtype="<i2"))
        for name, score in scores.items():
            if score >= self.threshold:
                log.debug("wake score %s %.2f", name, score)
                return name
        return None

    def reset(self) -> None:
        self.model.reset()


class WhisperDetector(Detector):
    """Transcribe short bursts of speech and look for the phrase in them.

    Only speech is transcribed, never silence, so on an idle desktop this does
    almost nothing. A burst is closed by silence or by getting too long, which
    keeps each transcription to well under a second of audio.
    """

    name = "whisper"

    def __init__(self, config):
        self.phrase = str(config.get("wakeword.phrase", "hey toony")).lower().strip()
        self.similarity = float(config.get("wakeword.similarity", 0.72))
        self.threshold = float(config.get("audio.energy_threshold", 0.012))
        self.max_burst = float(config.get("wakeword.max_burst_s", 2.5))
        self.silence_chunks = 4          # ~320 ms of quiet ends a burst
        self._buffer = bytearray()
        self._quiet = 0
        self._speaking = False
        self._model = None
        self._config = config
        self._words = [w for w in re.split(r"\W+", self.phrase) if w]

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("The whisper wake word needs faster-whisper: "
                               "pip install 'toony[local]'") from exc
        size = str(self._config.get("wakeword.whisper_model", "tiny.en"))
        device = str(self._config.get("stt.local.device", "auto"))
        if device == "auto":
            device = "cpu"      # the wake word must not fight the real STT for VRAM
        log.info("loading %s for wake-word spotting on %s", size, device)
        self._model = WhisperModel(size, device=device,
                                   compute_type="int8" if device == "cpu" else "float16")
        return self._model

    def check(self) -> str:
        try:
            self._load()
        except RuntimeError as exc:
            return str(exc)
        return f"listening for {self.phrase!r}"

    def feed(self, pcm: bytes) -> str | None:
        loud = _rms(pcm) >= self.threshold
        if loud:
            self._speaking = True
            self._quiet = 0
        elif self._speaking:
            self._quiet += 1

        if self._speaking:
            self._buffer += pcm

        too_long = len(self._buffer) > self.max_burst * RATE * 2
        ended = self._speaking and (self._quiet >= self.silence_chunks or too_long)
        if not ended:
            return None

        burst, self._buffer = bytes(self._buffer), bytearray()
        self._speaking, self._quiet = False, 0
        if len(burst) < 0.25 * RATE * 2:        # too short to be a phrase
            return None
        return self.phrase if self._matches(burst) else None

    def _matches(self, pcm: bytes) -> bool:
        import numpy as np

        audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        try:
            segments, _ = self._load().transcribe(
                audio, language="en", beam_size=1, without_timestamps=True,
                initial_prompt=self.phrase, vad_filter=False)
            heard = " ".join(segment.text for segment in segments)
        except Exception as exc:
            log.warning("wake-word transcription failed: %s", exc)
            return False
        return phrase_heard(heard, self.phrase, self.similarity)

    def reset(self) -> None:
        self._buffer.clear()
        self._speaking, self._quiet = False, 0


def _skeleton(word: str) -> str:
    """The consonants, collapsed. Whisper mangles vowels far more than these.

    "toony", "tunie", "toonie" and "tooney" all reduce to "tny"; "there" and
    "junie" do not, which is exactly the distinction that matters.
    """
    out = []
    for letter in word.lower():
        if letter in "aeiouy" or not letter.isalpha():
            continue
        if not out or out[-1] != letter:
            out.append(letter)
    return "".join(out) or word.lower()[:1]


def _similar(heard: str, target: str) -> float:
    return max(difflib.SequenceMatcher(None, heard, target).ratio(),
               difflib.SequenceMatcher(None, _skeleton(heard),
                                       _skeleton(target)).ratio())


def phrase_heard(heard: str, phrase: str, similarity: float = 0.72) -> bool:
    """Did this transcript contain the wake phrase?

    Speech recognition on a two-word burst is unreliable in predictable ways —
    "hey Toony" comes back as "hey tunie", "hey tony", "a tooney" — so what is
    compared is the name itself, both as written and as consonants, with the
    "hey" in front used only as corroboration for a weaker match.
    """
    words = re.sub(r"[^a-z0-9 ]+", " ", heard.lower()).split()
    target = phrase.lower().split()
    if not words or not target:
        return False

    name = target[-1]
    prefix = target[-2] if len(target) > 1 else ""
    for index, word in enumerate(words):
        score = _similar(word, name)
        if score >= 0.85:
            return True                 # unmistakable on its own
        if score < similarity:
            continue
        if not prefix:
            return True
        # The prefix is checked strictly, on the raw spelling only. It is a
        # short function word, so a loose match lets "the tuning" through as
        # "hey Toony"; a clearly-heard name does not need it anyway.
        before = words[index - 1] if index else ""
        if before and difflib.SequenceMatcher(None, before, prefix).ratio() >= 0.75:
            return True
    return False


def _rms(pcm: bytes) -> float:
    import numpy as np

    if not pcm:
        return 0.0
    samples = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


def build_detector(config) -> Detector:
    engine = str(config.get("wakeword.engine", "openwakeword")).lower()
    if engine in ("whisper", "faster-whisper", "local"):
        return WhisperDetector(config)
    if engine == "openwakeword":
        return OpenWakeWordDetector(config)
    raise RuntimeError(f"Unknown wake word engine {engine!r}. "
                       "Choose openwakeword or whisper.")


# ------------------------------------------------------------------ listener
class WakeWordListener:
    def __init__(self, config, on_wake: Callable[[str], None]):
        self.config = config
        self.on_wake = on_wake
        self.cooldown = float(config.get("wakeword.cooldown_s", 2.0))
        self._detector: Detector | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._paused = threading.Event()
        self._last_fire = 0.0

    def _load(self) -> Detector:
        if self._detector is None:
            self._detector = build_detector(self.config)
        return self._detector

    @property
    def engine(self) -> str:
        return self._detector.name if self._detector else str(
            self.config.get("wakeword.engine", "openwakeword"))

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._load()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="toony-wake",
                                        daemon=True)
        self._thread.start()
        log.info("listening for the wake word (%s)", self.engine)

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def pause(self) -> None:
        """Release the microphone while recording or speaking."""
        self._paused.set()

    def resume(self) -> None:
        if self._detector is not None:
            self._detector.reset()
        self._paused.clear()

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- the listening loop ----------------------------------------------
    def _loop(self) -> None:
        import sounddevice as sd

        from .devices import resolve

        device = resolve(self.config.get("audio.input_device", ""), want_input=True)
        detector = self._load()
        while self._running.is_set():
            if self._paused.is_set():
                time.sleep(0.1)
                continue
            try:
                with sd.RawInputStream(samplerate=RATE, blocksize=CHUNK,
                                       device=device, channels=1,
                                       dtype="int16") as stream:
                    while self._running.is_set() and not self._paused.is_set():
                        data, overflowed = stream.read(CHUNK)
                        if overflowed:
                            continue
                        heard = detector.feed(bytes(data))
                        if heard:
                            self._fire(heard)
            except Exception as exc:
                log.error("wake word stream failed: %s — retrying in 3s", exc)
                time.sleep(3)

    def _fire(self, name: str) -> None:
        now = time.monotonic()
        if now - self._last_fire < self.cooldown:
            return
        self._last_fire = now
        log.info("wake word %r heard", name)
        self.pause()
        try:
            self.on_wake(name)
        except Exception:
            log.exception("wake handler raised")
