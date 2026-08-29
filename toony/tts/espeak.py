"""espeak-ng: robotic, but it is in every distro and never needs a download."""

from __future__ import annotations

import io
import shutil
import subprocess
import wave

from .base import TTS, Speech, TTSError


class EspeakTTS(TTS):
    name = "espeak"

    def __init__(self, voice: str = "en", words_per_minute: int = 165,
                 speed: float = 1.0):
        self.voice = voice
        self.wpm = int(words_per_minute * max(0.1, speed))

    def synthesise(self, text: str) -> Speech:
        binary = shutil.which("espeak-ng") or shutil.which("espeak")
        if not binary:
            raise TTSError("espeak-ng is not installed")
        proc = subprocess.run(
            [binary, "-v", self.voice, "-s", str(self.wpm), "--stdout", text],
            capture_output=True, timeout=30)
        if proc.returncode != 0 or not proc.stdout:
            raise TTSError(proc.stderr.decode("utf-8", "replace").strip()
                           or "espeak failed")
        with wave.open(io.BytesIO(proc.stdout), "rb") as wav:
            return Speech(pcm=wav.readframes(wav.getnframes()),
                          sample_rate=wav.getframerate(),
                          channels=wav.getnchannels())

    def check(self) -> str:
        binary = shutil.which("espeak-ng") or shutil.which("espeak")
        return f"{binary} ready" if binary else "espeak-ng is not installed"
