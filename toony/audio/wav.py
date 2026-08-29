"""Conversions between the three shapes audio takes in Toony:
float32 frames (capture and VAD), 16-bit PCM (the STT/TTS interface), and WAV
containers (files and cloud APIs). Standard library only.
"""

from __future__ import annotations

import array
import io
import wave


def float_to_pcm16(samples) -> bytes:
    """Float32 in -1..1 to little-endian 16-bit PCM."""
    try:
        import numpy as np

        data = np.asarray(samples, dtype="float32")
        return (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    except ImportError:
        out = array.array("h")
        for value in samples:
            out.append(int(max(-1.0, min(1.0, float(value))) * 32767))
        return out.tobytes()


def pcm16_to_float(pcm: bytes):
    """16-bit PCM back to float32, for the wake word and VAD paths."""
    try:
        import numpy as np

        return np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    except ImportError:
        values = array.array("h")
        values.frombytes(pcm)
        return [v / 32768.0 for v in values]


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    """Read a WAV file into mono 16-bit PCM and its sample rate."""
    with wave.open(io.BytesIO(data), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("only 16-bit WAV audio is supported")
        rate = handle.getframerate()
        channels = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    if channels == 1:
        return frames, rate
    return _downmix(frames, channels), rate


def _downmix(frames: bytes, channels: int) -> bytes:
    values = array.array("h")
    values.frombytes(frames)
    mono = array.array("h")
    for index in range(0, len(values) - channels + 1, channels):
        mono.append(sum(values[index:index + channels]) // channels)
    return mono.tobytes()
