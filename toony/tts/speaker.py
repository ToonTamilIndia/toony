"""Turning a reply into sound.

Replies are spoken sentence by sentence: the first sentence starts playing while
the rest is still being synthesised, which is what makes the assistant feel fast.
"""

from __future__ import annotations

import queue
import threading

from ..log import get
from .base import TTS, TTSError, sentences

log = get("tts.speaker")


class Speaker:
    def __init__(self, tts: TTS, player, stream: bool = True):
        self.tts = tts
        self.player = player
        self.stream = stream
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self.player.stop()

    def say(self, text: str) -> None:
        """Speak a complete reply. Blocks until finished or interrupted."""
        if not text.strip():
            return
        if not self.stream:
            self._stop.clear()
            self._utter(text)
            return
        with self.open_stream() as feed:
            feed(text)

    def open_stream(self) -> "SpeechStream":
        """Speak text that is still being generated, sentence by sentence."""
        self._stop.clear()
        return SpeechStream(self, stream=self.stream)

    def _utter(self, text: str) -> None:
        try:
            speech = self.tts.synthesise(text)
        except TTSError as exc:
            log.error("synthesis failed: %s", exc)
            return
        self.player.play_pcm(speech.pcm, speech.sample_rate, speech.channels)


class SpeechStream:
    """Accepts text as it arrives and speaks each sentence once it is complete.

    Synthesis and playback run one sentence ahead of each other, which is what
    hides Piper's latency; feeding from a token stream hides the model's too.
    """

    def __init__(self, speaker: "Speaker", stream: bool = True):
        self.speaker = speaker
        self.stream = stream
        self._pending = ""
        self._spoke_anything = False
        self._queue: queue.Queue = queue.Queue(maxsize=2)
        self._worker = threading.Thread(target=self._synthesise_loop,
                                        name="toony-tts", daemon=True)
        self._player = threading.Thread(target=self._play_loop,
                                        name="toony-play", daemon=True)
        self._sentences: queue.Queue = queue.Queue()
        self._worker.start()
        self._player.start()

    def __enter__(self):
        return self.feed

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def spoke(self) -> bool:
        return self._spoke_anything

    def feed(self, chunk: str) -> None:
        """Add generated text. Complete sentences are queued for speech."""
        if self.speaker._stop.is_set():
            return
        self._pending += chunk
        parts = list(sentences(self._pending))
        if len(parts) > 1:
            # Hold the last part back: more text may still be coming for it.
            for sentence in parts[:-1]:
                self._sentences.put(sentence)
            self._pending = parts[-1]

    def close(self) -> None:
        """Flush the remaining text and wait for playback to finish."""
        for sentence in sentences(self._pending):
            self._sentences.put(sentence)
        self._pending = ""
        self._sentences.put(None)
        self._worker.join(timeout=120)
        self._player.join(timeout=120)

    def _synthesise_loop(self) -> None:
        try:
            while True:
                sentence = self._sentences.get()
                if sentence is None or self.speaker._stop.is_set():
                    break
                try:
                    self._queue.put(self.speaker.tts.synthesise(sentence))
                except TTSError as exc:
                    log.error("synthesis failed: %s", exc)
                    break
        finally:
            self._queue.put(None)

    def _play_loop(self) -> None:
        while True:
            speech = self._queue.get()
            if speech is None or self.speaker._stop.is_set():
                break
            self._spoke_anything = True
            self.speaker.player.play_pcm(speech.pcm, speech.sample_rate,
                                         speech.channels)
