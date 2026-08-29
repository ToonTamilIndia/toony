"""Audio device discovery, shared by the CLI and the capture path."""

from __future__ import annotations

from typing import Any


class AudioUnavailable(RuntimeError):
    """No usable audio stack — no PipeWire/ALSA, or sounddevice is missing."""


def _sd():
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise AudioUnavailable(
            "Audio needs the sounddevice package and a working PortAudio/PipeWire "
            "stack: pip install sounddevice"
        ) from exc
    return sd


def list_devices() -> list[dict[str, Any]]:
    sd = _sd()
    out = []
    try:
        defaults = sd.default.device
    except Exception:
        defaults = (None, None)
    for index, device in enumerate(sd.query_devices()):
        out.append({
            "index": index,
            "name": device["name"],
            "inputs": device["max_input_channels"],
            "outputs": device["max_output_channels"],
            "rate": int(device["default_samplerate"]),
            "default_in": index == defaults[0],
            "default_out": index == defaults[1],
        })
    return out


def resolve(name_or_index: str | int | None, want_input: bool) -> int | None:
    """Turn a configured device name into a PortAudio index. "" means default."""
    if name_or_index in ("", None):
        return None
    try:
        return int(name_or_index)
    except (TypeError, ValueError):
        pass
    needle = str(name_or_index).lower()
    for device in list_devices():
        channels = device["inputs"] if want_input else device["outputs"]
        if channels > 0 and needle in device["name"].lower():
            return device["index"]
    raise AudioUnavailable(f"No audio device matching {name_or_index!r}")
