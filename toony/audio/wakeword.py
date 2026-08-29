"""Wake-word listening ("hey Toony") with openWakeWord.

Runs its own microphone stream in a background thread and calls back when the
phrase is heard. The stream is released while Toony is recording or speaking, so
the assistant never hears itself.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from ..log import get

log = get("audio.wakeword")

CHUNK = 1280  # openWakeWord expects 80 ms of 16 kHz audio per call


class WakeWordListener:
    def __init__(self, config, on_wake: Callable[[str], None]):
        self.config = config
        self.on_wake = on_wake
        self.threshold = float(config.get("wakeword.threshold", 0.5))
        self.cooldown = float(config.get("wakeword.cooldown_s", 2.0))
        self._model = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._paused = threading.Event()
        self._last_fire = 0.0

    # ---- model ------------------------------------------------------------
    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError(
                "Wake word needs openwakeword: pip install 'toony[wake]'") from exc

        name = str(self.config.get("wakeword.model", "hey_jarvis"))
        directory = Path(str(self.config.get("wakeword.model_dir", ""))).expanduser()
        custom = directory / f"{name}.onnx"
        try:
            if custom.is_file():
                log.info("loading custom wake word model %s", custom)
                self._model = Model(wakeword_model_paths=[str(custom)])
            elif Path(name).is_file():
                self._model = Model(wakeword_model_paths=[name])
            else:
                import openwakeword
                bundled = (Path(openwakeword.__file__).parent
                           / "resources" / "models" / f"{name}.onnx")
                if bundled.is_file():
                    log.info("loading bundled wake word model %r", name)
                    self._model = Model(wakeword_model_paths=[str(bundled)])
                else:
                    matches = sorted(
                        (Path(openwakeword.__file__).parent
                         / "resources" / "models").glob(f"{name}*.onnx"))
                    if matches:
                        log.info("loading bundled wake word model %s", matches[0].name)
                        self._model = Model(wakeword_model_paths=[str(matches[0])])
                    else:
                        raise RuntimeError(f"no wake word model found for {name!r}")
        except Exception as exc:
            raise RuntimeError(f"could not load wake word model {name!r}: {exc}") from exc
        return self._model

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._load()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="toony-wake",
                                        daemon=True)
        self._thread.start()
        log.info("listening for the wake word")

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def pause(self) -> None:
        """Release the microphone while recording or speaking."""
        self._paused.set()

    def resume(self) -> None:
        if self._model is not None:
            self._model.reset()
        self._paused.clear()

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- the listening loop ----------------------------------------------
    def _loop(self) -> None:
        import numpy as np
        import sounddevice as sd

        from .devices import resolve

        device = resolve(self.config.get("audio.input_device", ""), want_input=True)
        model = self._load()
        while self._running.is_set():
            if self._paused.is_set():
                time.sleep(0.1)
                continue
            try:
                with sd.RawInputStream(samplerate=16000, blocksize=CHUNK,
                                       device=device, channels=1,
                                       dtype="int16") as stream:
                    while self._running.is_set() and not self._paused.is_set():
                        data, overflowed = stream.read(CHUNK)
                        if overflowed:
                            continue
                        audio = np.frombuffer(bytes(data), dtype="<i2")
                        scores = model.predict(audio)
                        for name, score in scores.items():
                            if score >= self.threshold:
                                self._fire(name, float(score))
            except Exception as exc:
                log.error("wake word stream failed: %s — retrying in 3s", exc)
                time.sleep(3)

    def _fire(self, name: str, score: float) -> None:
        now = time.monotonic()
        if now - self._last_fire < self.cooldown:
            return
        self._last_fire = now
        log.info("wake word %r heard (%.2f)", name, score)
        self.pause()
        try:
            self.on_wake(name)
        except Exception:
            log.exception("wake handler raised")
