"""Voice activity detection.

Two backends: a dependency-free energy gate that works everywhere, and webrtcvad
when it is installed. Both answer the same question — is this 20 ms frame speech?
"""

from __future__ import annotations

import math
from typing import Protocol


class VAD(Protocol):
    frame_ms: int

    def is_speech(self, frame: bytes, sample_rate: int) -> bool: ...


class EnergyVAD:
    """RMS gate with an adaptive noise floor.

    The floor tracks the quietest recent frames, so a noisy room raises the bar
    instead of making everything look like speech.
    """

    frame_ms = 20

    def __init__(self, threshold: float = 0.012, adapt: bool = True):
        self.threshold = threshold
        self.adapt = adapt
        self.floor = threshold / 3
        self._warmup = 0

    def rms(self, frame: bytes) -> float:
        if not frame:
            return 0.0
        count = len(frame) // 2
        if not count:
            return 0.0
        total = 0
        for index in range(0, count * 2, 2):
            sample = int.from_bytes(frame[index:index + 2], "little", signed=True)
            total += sample * sample
        return math.sqrt(total / count) / 32768.0

    def is_speech(self, frame: bytes, sample_rate: int = 16000) -> bool:
        level = self.rms(frame)
        speech = level > max(self.threshold, self.floor * 3)
        if self.adapt and not speech:
            # Slowly follow the background level while nobody is talking.
            self.floor = 0.95 * self.floor + 0.05 * level
            self._warmup += 1
        return speech


class WebrtcVAD:
    """Wrapper around webrtcvad. Only 8/16/32/48 kHz and 10/20/30 ms frames."""

    frame_ms = 20

    def __init__(self, aggressiveness: int = 2):
        import webrtcvad  # imported lazily; optional dependency

        self._vad = webrtcvad.Vad(int(max(0, min(3, aggressiveness))))

    def is_speech(self, frame: bytes, sample_rate: int = 16000) -> bool:
        expected = int(sample_rate * self.frame_ms / 1000) * 2
        if len(frame) != expected:
            return False
        return self._vad.is_speech(frame, sample_rate)


def build(config) -> VAD:
    kind = str(config.get("audio.vad", "energy")).lower()
    if kind == "webrtc":
        try:
            return WebrtcVAD(int(config.get("audio.vad_aggressiveness", 2)))
        except ImportError:
            pass  # fall through to the always-available energy gate
    return EnergyVAD(float(config.get("audio.energy_threshold", 0.012)))
