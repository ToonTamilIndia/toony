"""Audio conversions, endpointing and speech chunking — no hardware needed."""

from __future__ import annotations

import array
import math
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toony.audio.capture import Microphone
from toony.audio.vad import EnergyVAD, build as build_vad
from toony.audio.wav import (float_to_pcm16, pcm16_to_float, pcm_to_wav,
                             wav_to_pcm)
from toony.config import Config
from toony.tts.base import Speech, clean_for_speech, sentences


def tone(frames: int, amplitude: float = 0.3, rate: int = 16000) -> bytes:
    samples = array.array("h")
    for index in range(frames):
        samples.append(int(amplitude * 32767 * math.sin(2 * math.pi * 220 * index / rate)))
    return samples.tobytes()


def silence(frames: int) -> bytes:
    return b"\x00\x00" * frames


class TestConversions(unittest.TestCase):
    def test_float_pcm_round_trip(self):
        original = [0.0, 0.5, -0.5, 0.999, -0.999]
        restored = pcm16_to_float(float_to_pcm16(original))
        for before, after in zip(original, restored):
            self.assertAlmostEqual(before, after, places=3)

    def test_clipping_does_not_wrap_around(self):
        loud = pcm16_to_float(float_to_pcm16([5.0, -5.0]))
        self.assertGreater(loud[0], 0.9)
        self.assertLess(loud[1], -0.9)

    def test_wav_round_trip(self):
        pcm = tone(800)
        data = pcm_to_wav(pcm, 16000)
        self.assertEqual(data[:4], b"RIFF")
        restored, rate = wav_to_pcm(data)
        self.assertEqual(rate, 16000)
        self.assertEqual(restored, pcm)

    def test_stereo_is_downmixed_to_mono(self):
        stereo = array.array("h", [100, 300] * 50).tobytes()
        mono, _ = wav_to_pcm(pcm_to_wav(stereo, 8000, channels=2))
        values = array.array("h")
        values.frombytes(mono)
        self.assertEqual(len(values), 50)
        self.assertEqual(values[0], 200)


class TestVAD(unittest.TestCase):
    def test_energy_gate_separates_speech_from_silence(self):
        vad = EnergyVAD()
        self.assertTrue(vad.is_speech(tone(320), 16000))
        self.assertFalse(vad.is_speech(silence(320), 16000))

    def test_noise_floor_adapts_upward(self):
        vad = EnergyVAD()
        quiet_hiss = float_to_pcm16([0.004 * math.sin(i) for i in range(320)])
        for _ in range(200):
            vad.is_speech(quiet_hiss, 16000)
        self.assertGreater(vad.floor, 0.0)
        self.assertTrue(vad.is_speech(tone(320), 16000))

    def test_builder_falls_back_when_webrtcvad_is_absent(self):
        config = Config()
        config.set("audio.vad", "webrtc", save=False)
        self.assertTrue(hasattr(build_vad(config), "is_speech"))


class TestEndpointing(unittest.TestCase):
    """record_utterance() reads from the frame queue, so it is testable."""

    def _microphone(self, **overrides):
        config = Config()
        config.set("audio.silence_ms", 100, save=False)
        config.set("audio.min_utterance_ms", 40, save=False)
        for key, value in overrides.items():
            config.set(key, value, save=False)
        return Microphone(config)

    def _feed(self, mic, frames):
        """Deliver frames the way the audio callback does: after recording starts.

        record_utterance() drains stale audio first, so anything queued before
        the call is deliberately thrown away.
        """
        def push():
            time.sleep(0.05)
            for frame in frames:
                mic._queue.put(frame)

        thread = threading.Thread(target=push, daemon=True)
        thread.start()
        return thread

    def test_stops_after_silence(self):
        mic = self._microphone()
        size = mic.frame_samples
        self._feed(mic, [tone(size)] * 10 + [silence(size)] * 10)
        pcm = mic.record_utterance()
        self.assertIsNotNone(pcm)
        # Ten voiced frames plus the silence that ended the utterance.
        self.assertGreaterEqual(len(pcm) // (size * 2), 10)

    def test_returns_none_when_nobody_speaks(self):
        mic = self._microphone()
        self._feed(mic, [silence(mic.frame_samples)] * 200)
        self.assertIsNone(mic.record_utterance())

    def test_stop_event_ends_recording(self):
        mic = self._microphone()
        stop = threading.Event()
        stop.set()
        self._feed(mic, [tone(mic.frame_samples)] * 10)
        self.assertIsNone(mic.record_utterance(stop_event=stop))

    def test_short_noise_is_rejected_as_too_brief(self):
        mic = self._microphone(**{"audio.min_utterance_ms": 400})
        size = mic.frame_samples
        self._feed(mic, [tone(size)] * 2 + [silence(size)] * 10)
        self.assertIsNone(mic.record_utterance())

    def test_max_length_is_enforced(self):
        mic = self._microphone(**{"audio.max_utterance_s": 0.2})
        size = mic.frame_samples
        self._feed(mic, [tone(size)] * 100)
        pcm = mic.record_utterance()
        self.assertIsNotNone(pcm)
        self.assertLessEqual(len(pcm) // (size * 2), 12)

    def test_drain_discards_stale_audio(self):
        mic = self._microphone()
        for _ in range(5):
            mic._queue.put(tone(mic.frame_samples))
        mic.drain()
        self.assertTrue(mic._queue.empty())


class TestSpeechChunking(unittest.TestCase):
    def test_sentences_do_not_split_on_abbreviations(self):
        text = "Dr. Smith called. He said it is fine! Did you know? Yes."
        self.assertEqual(list(sentences(text)),
                         ["Dr. Smith called.", "He said it is fine!",
                          "Did you know?", "Yes."])

    def test_markdown_is_stripped_before_speaking(self):
        spoken = clean_for_speech("**Bold** and `code`\n- a bullet\n## Heading\n"
                                  "See https://example.com/x for more")
        self.assertNotIn("*", spoken)
        self.assertNotIn("#", spoken)
        self.assertNotIn("https://", spoken)
        self.assertIn("a link", spoken)

    def test_very_long_sentences_are_split_on_commas(self):
        long_sentence = ", ".join(["a fairly long clause of words"] * 20) + "."
        chunks = list(sentences(long_sentence))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 300 for chunk in chunks))

    def test_speech_carries_its_own_sample_rate(self):
        speech = Speech(pcm=b"\x00\x00", sample_rate=22050)
        self.assertEqual(speech.channels, 1)
        self.assertEqual(speech.sample_rate, 22050)


if __name__ == "__main__":
    unittest.main(verbosity=2)
