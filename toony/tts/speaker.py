"""Turning a reply into sound.

Replies are spoken sentence by sentence: the first sentence starts playing while
the rest is still being synthesised, which is what makes the assistant feel fast.
"""

from __future__ import annotations

import queue
import threading

from ..log import get
from ..text import clip_for_speech, speakable
from .base import TTS, TTSError, sentences

log = get("tts.speaker")


class Speaker:
    def __init__(self, tts: TTS, player, stream: bool = True,
                 clean: bool = True, max_chars: int = 0):
        self.tts = tts
        self.player = player
        self.stream = stream
        self.clean = clean
        self.max_chars = max_chars
        self._stop = threading.Event()

    @classmethod
    def from_config(cls, tts: TTS, player, config) -> "Speaker":
        return cls(tts, player,
                   stream=bool(config.get("tts.stream", True)),
                   clean=bool(config.get("tts.speakable", True)),
                   max_chars=int(config.get("tts.max_spoken_chars", 700)))

    def prepare(self, text: str) -> str:
        """What actually reaches the synthesiser, as opposed to the screen."""
        return speakable(text) if self.clean else text

    def stop(self) -> None:
        self._stop.set()
        self.player.stop()

    def say(self, text: str) -> None:
        """Speak a complete reply. Blocks until finished or interrupted."""
        if not text.strip():
            return
        spoken, was_cut = clip_for_speech(text, self.max_chars)
        if was_cut:
            log.info("reply was %d characters — speaking the first %d",
                     len(text), len(spoken))
            spoken += " The rest is on screen."
        if not self.stream:
            self._stop.clear()
            self._utter(spoken)
            return
        with self.open_stream() as feed:
            feed(spoken)

    def open_stream(self) -> "SpeechStream":
        """Speak text that is still being generated, sentence by sentence."""
        self._stop.clear()
        return SpeechStream(self, stream=self.stream)

    def _utter(self, text: str) -> None:
        text = self.prepare(text)
        if not text.strip():
            return
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
        self._spoken_chars = 0
        self._cut = False
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

    @property
    def cut_short(self) -> bool:
        return self._cut

    def feed(self, chunk: str) -> None:
        """Add generated text. Complete sentences are queued for speech."""
        if self.speaker._stop.is_set() or self._cut:
            return
        self._pending += chunk
        parts = list(sentences(self._pending))
        if len(parts) > 1:
            # Hold the last part back: more text may still be coming for it.
            for sentence in parts[:-1]:
                if not self._offer(sentence):
                    return
            self._pending = parts[-1]

    def _offer(self, sentence: str) -> bool:
        """Queue one sentence unless the spoken length budget is used up.

        The model may keep generating; there is no point speaking four
        paragraphs at somebody. The window shows all of it either way.
        """
        limit = self.speaker.max_chars
        if limit and self._spoken_chars + len(sentence) > limit:
            self._cut = True
            self._sentences.put("The rest is on screen.")
            self._sentences.put(None)
            return False
        self._spoken_chars += len(sentence)
        self._sentences.put(sentence)
        return True

    def close(self) -> None:
        """Flush the remaining text and wait for playback to finish."""
        if not self._cut:
            for sentence in sentences(self._pending):
                if not self._offer(sentence):
                    break
            self._sentences.put(None)
        self._pending = ""
        self._worker.join(timeout=120)
        self._player.join(timeout=120)

    def _synthesise_loop(self) -> None:
        try:
            while True:
                sentence = self._sentences.get()
                if sentence is None or self.speaker._stop.is_set():
                    break
                spoken = self.speaker.prepare(sentence)
                if not spoken.strip():
                    continue
                try:
                    self._queue.put(self.speaker.tts.synthesise(spoken))
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
