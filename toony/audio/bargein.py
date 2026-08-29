"""Interrupting Toony by talking over it.

The hard part is not hearing you — it is not hearing itself. The microphone
picks up the speakers, so a plain voice-activity check fires on Toony's own
reply within a syllable. Three things keep that from happening:

* a threshold well above the one used for ordinary listening, so speaker bleed
  at normal volume does not reach it,
* a run of consecutive speech frames, so a door or a keystroke is not a person,
* a grace period at the start, so the chime and the first word are ignored.

With headphones it is exact. Over laptop speakers, turn the volume down or
raise ``audio.barge_in_sensitivity`` until it stops triggering on itself.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from ..log import get
from .devices import AudioUnavailable, resolve

log = get("audio.bargein")

FRAME_MS = 30


class BargeInListener:
    """Listens while Toony speaks, and calls back the moment you cut in."""

    def __init__(self, config, on_speech: Callable[[], None]):
        self.config = config
        self.on_speech = on_speech
        self.sample_rate = int(config.get("audio.sample_rate", 16000))
        self.frame_samples = int(self.sample_rate * FRAME_MS / 1000)

        base = float(config.get("audio.energy_threshold", 0.012))
        sensitivity = float(config.get("audio.barge_in_sensitivity", 2.5))
        self.threshold = base * max(1.0, sensitivity)
        self.needed = max(1, int(float(config.get("audio.barge_in_ms", 300))
                                 / FRAME_MS))
        self.grace = float(config.get("audio.barge_in_grace_ms", 600)) / 1000.0

        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._fired = threading.Event()

    @property
    def fired(self) -> bool:
        return self._fired.is_set()

    def __enter__(self) -> "BargeInListener":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._fired.clear()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="toony-bargein",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None

    # ---- the listening loop ----------------------------------------------
    def _loop(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            log.info("barge-in unavailable: %s", exc)
            return

        try:
            device = resolve(self.config.get("audio.input_device", ""),
                             want_input=True)
        except AudioUnavailable as exc:
            log.info("barge-in unavailable: %s", exc)
            return

        opened = time.monotonic()
        voiced = 0
        try:
            with sd.RawInputStream(samplerate=self.sample_rate,
                                   blocksize=self.frame_samples, device=device,
                                   channels=1, dtype="int16") as stream:
                while self._running.is_set():
                    data, overflowed = stream.read(self.frame_samples)
                    if overflowed or time.monotonic() - opened < self.grace:
                        continue
                    samples = np.frombuffer(bytes(data), dtype="<i2")
                    if samples.size == 0:
                        continue
                    level = float(np.sqrt(np.mean(
                        (samples.astype("float32") / 32768.0) ** 2)))
                    voiced = voiced + 1 if level >= self.threshold else 0
                    if voiced >= self.needed:
                        self._fire(level)
                        return
        except Exception as exc:
            log.debug("barge-in stream ended: %s", exc)

    def _fire(self, level: float) -> None:
        if self._fired.is_set():
            return
        self._fired.set()
        log.info("heard you talking over it (level %.3f) — stopping", level)
        try:
            self.on_speech()
        except Exception:
            log.exception("barge-in handler raised")


def build(config, on_speech: Callable[[], None]) -> BargeInListener | None:
    """A listener, or None when barge-in is switched off."""
    if not config.get("audio.barge_in", True):
        return None
    return BargeInListener(config, on_speech)
