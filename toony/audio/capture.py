"""Microphone capture with endpointing.

Everything here is 16-bit PCM: it is what the VAD, the wake word model and the
speech-to-text backends all take, so no conversion happens on the hot path.

Recording ends when the speaker goes quiet for ``audio.silence_ms``, when the
caller sets the stop event (push-to-talk release), or at ``max_utterance_s``.
"""

from __future__ import annotations

import queue
import threading
import time

from ..log import get
from .devices import AudioUnavailable, list_devices, resolve  # noqa: F401
from .vad import build as build_vad

log = get("audio.capture")


def _sounddevice():
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise AudioUnavailable(
            "Audio input needs the sounddevice package and a working PortAudio "
            "or PipeWire setup: pip install sounddevice"
        ) from exc
    return sd


class Microphone:
    """A live input stream that hands out fixed-size PCM frames."""

    def __init__(self, config):
        self.config = config
        self.sample_rate = int(config.get("audio.sample_rate", 16000))
        self.vad = build_vad(config)
        self.frame_ms = self.vad.frame_ms
        self.frame_samples = int(self.sample_rate * self.frame_ms / 1000)
        self.device = resolve(config.get("audio.input_device", ""), want_input=True)
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stream = None

    def __enter__(self) -> "Microphone":
        sd = _sounddevice()

        def callback(indata, frames, time_info, status):
            if status:
                log.debug("input stream status: %s", status)
            try:
                self._queue.put_nowait(bytes(indata))
            except queue.Full:
                pass  # dropping a frame beats blocking the audio callback

        try:
            # RawInputStream hands back bytes directly, with no numpy in between.
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate, blocksize=self.frame_samples,
                device=self.device, channels=1, dtype="int16", callback=callback)
            self._stream.start()
        except Exception as exc:
            raise AudioUnavailable(f"could not open the microphone: {exc}") from exc
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

    def frames(self, timeout: float = 1.0):
        """Yield PCM frames as they arrive; returns if the stream goes quiet."""
        while True:
            try:
                yield self._queue.get(timeout=timeout)
            except queue.Empty:
                return

    def drain(self) -> None:
        """Throw away buffered audio from before the user was asked to speak."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    # ---- the important one ------------------------------------------------
    def record_utterance(self, stop_event: threading.Event | None = None,
                         wait_for_speech: bool = True,
                         max_seconds: float | None = None,
                         lead_in_s: float = 4.0) -> bytes | None:
        """Record one utterance as 16-bit PCM. None if nothing was said.

        ``max_seconds`` overrides the configured ceiling. A yes/no answer needs
        a few seconds, not thirty — waiting the full length for a word that
        never comes is half a minute of the user wondering what happened.
        """
        config = self.config
        per_second = 1000 / self.frame_ms
        silence_frames = max(1, int(int(config.get("audio.silence_ms", 800))
                                    / self.frame_ms))
        min_frames = max(1, int(int(config.get("audio.min_utterance_ms", 350))
                                / self.frame_ms))
        ceiling = (max_seconds if max_seconds is not None
                   else float(config.get("audio.max_utterance_s", 30)))
        max_frames = int(ceiling * per_second)
        lead_in_frames = int(lead_in_s * per_second)

        collected: list[bytes] = []
        quiet = waited = 0
        speaking = not wait_for_speech
        self.drain()
        started = time.monotonic()

        for frame in self.frames(timeout=1.0):
            if stop_event is not None and stop_event.is_set():
                break
            voiced = self.vad.is_speech(frame, self.sample_rate)

            if not speaking:
                waited += 1
                if voiced:
                    speaking = True
                    collected.append(frame)
                elif waited > lead_in_frames:
                    log.info("no speech detected")
                    return None
                continue

            collected.append(frame)
            quiet = 0 if voiced else quiet + 1
            if quiet >= silence_frames and len(collected) > min_frames:
                break
            if len(collected) >= max_frames:
                log.info("hit the maximum utterance length")
                break

        if len(collected) < min_frames:
            return None
        log.info("captured %.1fs of audio", time.monotonic() - started)
        return b"".join(collected)
