"""Speaker output. Playback is interruptible so the user can talk over Toony."""

from __future__ import annotations

import math
import threading

from ..log import get
from .devices import AudioUnavailable, resolve
from .wav import pcm16_to_float

log = get("audio.playback")


class Player:
    """Plays 16-bit PCM through PortAudio. ``stop()`` cuts it off immediately."""

    def __init__(self, config):
        self._configured = config.get("audio.output_device", "")
        self._device = None
        self._resolved = False
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def _device_index(self):
        if not self._resolved:
            try:
                self._device = resolve(self._configured, want_input=False)
            except AudioUnavailable as exc:
                log.warning("%s — using the default output", exc)
                self._device = None
            self._resolved = True
        return self._device

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def play_pcm(self, pcm: bytes, sample_rate: int, channels: int = 1) -> bool:
        """Play PCM. Returns False if it was interrupted or could not play."""
        if not pcm:
            return True
        try:
            import numpy as np
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            log.error("cannot play audio: %s", exc)
            return False

        data = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        data = data.reshape(-1, channels) if channels > 1 else data.reshape(-1, 1)
        # 1024 frames is short enough that stop() feels instant.
        block = 1024
        with self._lock:
            self._stop.clear()
            try:
                with sd.OutputStream(samplerate=sample_rate, channels=channels,
                                     dtype="float32",
                                     device=self._device_index()) as stream:
                    for start in range(0, len(data), block):
                        if self._stop.is_set():
                            log.info("playback interrupted")
                            return False
                        stream.write(data[start:start + block])
            except Exception as exc:
                log.error("playback failed: %s", exc)
                return False
        return True

    def play_float(self, samples, sample_rate: int) -> bool:
        from .wav import float_to_pcm16

        return self.play_pcm(float_to_pcm16(samples), sample_rate)

    def chime(self, kind: str = "start") -> None:
        """A short tone so the user knows Toony started or stopped listening."""
        frequency = 880.0 if kind == "start" else 587.33
        self.play_pcm(_tone(frequency), 44100)


def _tone(frequency: float, duration: float = 0.09, rate: int = 44100) -> bytes:
    """A short sine with fade in and out, built without numpy."""
    import array

    samples = array.array("h")
    total = int(rate * duration)
    for index in range(total):
        # Ramp over the first and last 3 ms so it does not click.
        envelope = min(1.0, index / (rate * 0.003),
                       (total - index) / (rate * 0.003))
        samples.append(int(0.18 * envelope * 32767
                           * math.sin(2 * math.pi * frequency * index / rate)))
    return samples.tobytes()
