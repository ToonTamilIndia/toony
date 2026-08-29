"""Local neural text-to-speech with Piper."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..log import get
from .base import TTS, Speech, TTSError

log = get("tts.piper")

VOICE_BASE = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
              "{lang}/{region}/{name}/{quality}/{voice}")


class PiperTTS(TTS):
    name = "piper"

    def __init__(self, voice: str, model_dir: str, binary: str = "piper",
                 speed: float = 1.0):
        self.voice = voice
        self.model_dir = Path(model_dir).expanduser()
        self.binary = binary
        self.speed = float(speed)
        self._resolved: Path | None = None

    # ---- model files ------------------------------------------------------
    def model_path(self) -> Path:
        """Locate the .onnx for the configured voice."""
        if self._resolved and self._resolved.exists():
            return self._resolved
        candidate = Path(self.voice).expanduser()
        if candidate.is_file():
            self._resolved = candidate
            return candidate
        candidate = self.model_dir / f"{self.voice}.onnx"
        if candidate.is_file():
            self._resolved = candidate
            return candidate
        matches = sorted(self.model_dir.glob(f"{self.voice}*.onnx"))
        if matches:
            self._resolved = matches[0]
            return matches[0]
        raise TTSError(
            f"Piper voice {self.voice!r} is not installed. "
            f"Download it with: toony voices install {self.voice}")

    def sample_rate(self) -> int:
        config_path = Path(str(self.model_path()) + ".json")
        try:
            with open(config_path, encoding="utf-8") as fh:
                return int(json.load(fh)["audio"]["sample_rate"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return 22050  # every published Piper voice so far

    # ---- synthesis --------------------------------------------------------
    def synthesise(self, text: str) -> Speech:
        if not text.strip():
            return Speech(b"", self.sample_rate())
        if not shutil.which(self.binary):
            raise TTSError(f"{self.binary} is not installed. "
                           "Install it with: pip install piper-tts")
        model = self.model_path()
        argv = [self.binary, "--model", str(model), "--output_raw"]
        if abs(self.speed - 1.0) > 0.01:
            # Piper's length scale is inverse to speed.
            argv += ["--length_scale", f"{1.0 / max(0.1, self.speed):.3f}"]
        try:
            proc = subprocess.run(argv, input=text.encode("utf-8"),
                                  capture_output=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise TTSError("Piper timed out") from exc
        if proc.returncode != 0:
            raise TTSError(proc.stderr.decode("utf-8", "replace").strip()
                           or "piper failed")
        return Speech(pcm=proc.stdout, sample_rate=self.sample_rate())

    def warm(self) -> None:
        try:
            self.model_path()
        except TTSError as exc:
            log.warning("%s", exc)

    def check(self) -> str:
        if not shutil.which(self.binary):
            return f"{self.binary} is not installed (pip install piper-tts)"
        try:
            model = self.model_path()
        except TTSError as exc:
            return str(exc)
        return f"piper ready, voice {model.name} at {self.sample_rate()} Hz"


def voice_url(voice: str) -> tuple[str, str]:
    """Build the download URLs for a voice name like en_US-amy-medium."""
    try:
        locale, name, quality = voice.split("-", 2)
        lang = locale.split("_")[0]
    except ValueError as exc:
        raise TTSError(f"{voice!r} is not a valid voice name "
                       "(expected something like en_US-amy-medium)") from exc
    base = VOICE_BASE.format(lang=lang, region=locale, name=name,
                             quality=quality, voice=f"{voice}.onnx")
    return base, base + ".json"
