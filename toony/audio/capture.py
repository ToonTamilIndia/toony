"""Microphone capture with endpointing, and a memory of the last second.

Everything here is 16-bit PCM: it is what the VAD, the wake word model and the
speech-to-text backends all take, so no conversion happens on the hot path.

Two things make push-to-talk feel instant rather than merely fast:

**The stream stays open.** Opening a PortAudio stream costs somewhere between
fifty and four hundred milliseconds depending on what PipeWire is doing. Paying
that *after* the key is pressed means the microphone starts listening after you
have started speaking. So the stream is opened once and kept, and the key press
only has to say "start collecting".

**Pre-roll.** Even with the stream open there is a gap: the hotkey travels
through the compositor, a socket and a thread before anything here hears about
it. So the last ``audio.preroll_ms`` of audio is always kept in a ring buffer,
and when recording starts it is used as the beginning of the utterance. The
effect is that speech from *before* the key press is captured, which is the
only way the first syllable is ever reliably there.

Recording ends when the speaker goes quiet for ``audio.silence_ms``, when the
caller sets the stop event (push-to-talk release), or at ``max_utterance_s``.
"""

from __future__ import annotations

import collections
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
    """A live input stream that hands out fixed-size PCM frames.

    Usable as a context manager for a single utterance, or opened once and kept
    for the life of the process — :meth:`open` and :meth:`close` are both
    idempotent, and reopening after a device change is fine.
    """

    def __init__(self, config):
        self.config = config
        self.sample_rate = int(config.get("audio.sample_rate", 16000))
        self.vad = build_vad(config)
        self.frame_ms = self.vad.frame_ms
        self.frame_samples = int(self.sample_rate * self.frame_ms / 1000)
        self.device = resolve(config.get("audio.input_device", ""), want_input=True)

        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stream = None
        self._lock = threading.RLock()
        self.opened_at = 0.0
        self.last_used = 0.0
        self.open_ms = 0.0

        preroll_ms = max(0, int(config.get("audio.preroll_ms", 700)))
        self._preroll_frames = int(preroll_ms / self.frame_ms) if preroll_ms else 0
        # A deque with a maxlen is its own ring buffer, and append from the
        # audio callback is atomic, so this needs no lock on the hot path.
        self._preroll: collections.deque = collections.deque(
            maxlen=self._preroll_frames or 1)
        self._keep_preroll = self._preroll_frames > 0

    # ---- opening and closing ---------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def __enter__(self) -> "Microphone":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def open(self) -> "Microphone":
        """Start the stream if it is not already running."""
        with self._lock:
            if self._stream is not None:
                self.last_used = time.monotonic()
                return self
            sd = _sounddevice()
            started = time.monotonic()

            def callback(indata, frames, time_info, status):
                if status:
                    log.debug("input stream status: %s", status)
                frame = bytes(indata)
                if self._keep_preroll:
                    self._preroll.append(frame)
                try:
                    self._queue.put_nowait(frame)
                except queue.Full:
                    # Nobody is recording, or the consumer fell behind.
                    # Dropping a frame beats blocking the audio callback.
                    pass

            try:
                # RawInputStream hands back bytes directly, with no numpy in
                # between.
                self._stream = sd.RawInputStream(
                    samplerate=self.sample_rate, blocksize=self.frame_samples,
                    device=self.device, channels=1, dtype="int16",
                    callback=callback)
                self._stream.start()
            except Exception as exc:
                self._stream = None
                raise AudioUnavailable(
                    f"could not open the microphone: {exc}") from exc
            self.open_ms = (time.monotonic() - started) * 1000
            self.opened_at = self.last_used = time.monotonic()
            log.debug("microphone open in %.0fms", self.open_ms)
            return self

    def close(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            self._preroll.clear()
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("closing the input stream failed", exc_info=True)

    def idle_for(self) -> float:
        """Seconds since this microphone was last used for anything."""
        return time.monotonic() - self.last_used if self.is_open else 0.0

    # ---- frames -----------------------------------------------------------
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

    def preroll(self) -> list[bytes]:
        """A snapshot of the audio just before now, oldest first."""
        return list(self._preroll)

    def clear_preroll(self) -> None:
        """Forget what was heard — used when it was Toony's own voice."""
        self._preroll.clear()

    # ---- the important one ------------------------------------------------
    def record_utterance(self, stop_event: threading.Event | None = None,
                         wait_for_speech: bool = True,
                         max_seconds: float | None = None,
                         lead_in_s: float = 4.0,
                         use_preroll: bool = True) -> bytes | None:
        """Record one utterance as 16-bit PCM. None if nothing was said.

        ``max_seconds`` overrides the configured ceiling. A yes/no answer needs
        a few seconds, not thirty — waiting the full length for a word that
        never comes is half a minute of the user wondering what happened.

        ``use_preroll`` prepends the audio captured just before the call, so a
        word begun before the key press is not lost. Turn it off when the
        buffer holds something other than the user: after barge-in it is full
        of Toony's own reply.

        The stream must already be open — :class:`MicrophonePool` is what
        arranges that, and keeping the two apart is what lets one open stream
        serve many utterances.
        """
        self.last_used = time.monotonic()
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

        # Order matters: take the pre-roll first, then throw away the queue.
        # The other way round would drop the newest frames on the floor.
        lead = self.preroll() if use_preroll else []
        self.drain()
        collected: list[bytes] = []
        quiet = waited = 0
        speaking = not wait_for_speech
        started = time.monotonic()

        if lead:
            # Only the tail of the buffer that actually has speech in it —
            # otherwise every utterance starts with a second of room tone,
            # which the decoder is happy to hallucinate words out of.
            voiced_lead = self._trim_lead(lead)
            if voiced_lead:
                collected.extend(voiced_lead)
                speaking = True
                log.debug("kept %d pre-roll frames (%.0fms)", len(voiced_lead),
                          len(voiced_lead) * self.frame_ms)

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

        self.last_used = time.monotonic()
        if len(collected) < min_frames:
            return None
        log.info("captured %.1fs of audio", time.monotonic() - started)
        return b"".join(collected)

    def _trim_lead(self, lead: list[bytes]) -> list[bytes]:
        """The pre-roll from the first speech frame onwards, plus a little air.

        A couple of frames of silence in front of a word helps the decoder more
        than it hurts; a whole second of it does not.
        """
        for index, frame in enumerate(lead):
            if self.vad.is_speech(frame, self.sample_rate):
                return lead[max(0, index - 2):]
        return []


class MicrophonePool:
    """One microphone, opened on demand and let go when nobody wants it.

    The point of keeping the stream open is latency; the point of eventually
    closing it is that an input stream held forever shows up as a recording
    indicator, and on some setups stops anything else using the microphone.
    Between the two, a timer.
    """

    def __init__(self, config):
        self.config = config
        self._mic: Microphone | None = None
        self._lock = threading.Lock()

    @property
    def keep_open(self) -> bool:
        return bool(self.config.get("audio.keep_stream_open", True))

    @property
    def idle_timeout(self) -> float:
        return float(self.config.get("audio.stream_idle_s", 120))

    @property
    def is_open(self) -> bool:
        return bool(self._mic is not None and self._mic.is_open)

    def acquire(self) -> Microphone:
        """A microphone with a running stream."""
        with self._lock:
            if self._mic is None:
                self._mic = Microphone(self.config)
            self._mic.open()
            return self._mic

    def release(self) -> None:
        """Done for now. Keeps the stream if that is what we were asked to do."""
        with self._lock:
            if self._mic is None:
                return
            if not self.keep_open:
                self._mic.close()

    def warm(self) -> bool:
        """Open the stream ahead of the first key press. False if it could not."""
        if not self.keep_open:
            return False
        try:
            self.acquire()
            return True
        except AudioUnavailable as exc:
            log.info("could not warm the microphone: %s", exc)
            return False

    def reap(self) -> bool:
        """Close the stream if it has been idle too long. True if it closed."""
        with self._lock:
            mic = self._mic
            timeout = self.idle_timeout
            if mic is None or not mic.is_open or timeout <= 0:
                return False
            if mic.idle_for() < timeout:
                return False
            log.debug("microphone idle for %.0fs — releasing the device",
                      mic.idle_for())
            mic.close()
            return True

    def reset(self) -> None:
        """Drop the microphone entirely, so the next use re-reads the device."""
        with self._lock:
            if self._mic is not None:
                self._mic.close()
            self._mic = None

    def close(self) -> None:
        self.reset()
