"""The Telegram bridge, parallel tool execution, barge-in and wake-word loading."""

from __future__ import annotations

import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toony.agent import Agent
from toony.audio.wakeword import OpenWakeWordDetector, _openwakeword_paths
from toony.brain.base import Brain, BrainReply, ToolCall
from toony.bridges.telegram import (WIRE_LIMIT, TelegramBridge, TelegramError,
                                    call, split_message)
from toony.config import Config
from toony.tools import REGISTRY
from toony.tools.registry import Tool


def make_config(**overrides):
    cfg = Config()
    cfg.set("conversation.persist", False, save=False)
    for key, value in overrides.items():
        cfg.set(key.replace("__", "."), value, save=False)
    return cfg


# --------------------------------------------------------------- telegram
class FakeTelegram:
    """Stands in for the Bot API: records what was sent, replays what arrives."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edited: list[dict] = []

    def call(self, token, method, http_timeout=15.0, **params):
        if method == "sendMessage":
            self.sent.append(params)
            return {"message_id": len(self.sent)}
        if method == "editMessageText":
            self.edited.append(params)
            return {"message_id": params.get("message_id")}
        if method == "getMe":
            return {"username": "toony_bot", "first_name": "Toony"}
        raise AssertionError(f"unexpected method {method}")

    def texts(self) -> list[str]:
        return [m.get("text", "") for m in self.sent]


def message(chat="42", text="hello", who="Ragul", update_id=1):
    return {"update_id": update_id,
            "message": {"chat": {"id": chat}, "text": text,
                        "from": {"first_name": who}}}


class TestSplitting(unittest.TestCase):
    def test_a_short_reply_is_one_message(self):
        self.assertEqual(split_message("hello"), ["hello"])

    def test_nothing_produces_nothing(self):
        self.assertEqual(split_message(""), [])

    def test_a_long_reply_is_split_under_the_wire_limit(self):
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(300))
        parts = split_message(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= WIRE_LIMIT for p in parts))

    def test_a_single_enormous_line_is_still_split(self):
        parts = split_message("y" * 10000)
        self.assertTrue(all(len(p) <= WIRE_LIMIT for p in parts))
        self.assertEqual(sum(len(p) for p in parts), 10000)


class TestPairing(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegram()
        self.answered: list[str] = []
        self.config = make_config(telegram__token="1:x",
                                  telegram__pairing_code="ABC123",
                                  telegram__allowed_chats=[])
        self.bridge = TelegramBridge(self.config, self._answer,
                                     save=lambda k, v: None)

    def _answer(self, text, meta):
        self.answered.append(text)
        return f"answered {text}"

    def _handle(self, *updates):
        with mock.patch("toony.bridges.telegram.call", self.api.call):
            self.bridge._handle_batch(list(updates))

    def test_an_unpaired_chat_is_refused_not_answered(self):
        """A bot token is a public URL. Without this, leaking it is fatal."""
        self._handle(message(text="delete everything"))
        self.assertEqual(self.answered, [])
        self.assertIn("not paired", self.api.texts()[0])

    def test_the_pairing_code_admits_a_chat(self):
        self._handle(message(text="ABC123"))
        self.assertIn("42", self.config.get("telegram.allowed_chats"))
        self.assertIn("Paired", self.api.texts()[0])

    def test_a_wrong_code_does_not(self):
        self._handle(message(text="ABC124"))
        self.assertEqual(self.config.get("telegram.allowed_chats"), [])

    def test_a_paired_chat_reaches_the_assistant(self):
        self.config.set("telegram.allowed_chats", ["42"], save=False)
        self._handle(message(text="what time is it"))
        self.assertEqual(self.answered, ["what time is it"])
        self.assertIn("answered what time is it",
                      [e.get("text") for e in self.api.edited])

    def test_another_chat_is_still_refused_after_one_is_paired(self):
        self.config.set("telegram.allowed_chats", ["42"], save=False)
        self._handle(message(chat="99", text="hello"))
        self.assertEqual(self.answered, [])
        self.assertIn("not paired", self.api.texts()[0])


class TestLimits(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegram()
        self.answered: list[str] = []
        self.config = make_config(telegram__token="1:x",
                                  telegram__allowed_chats=["42"],
                                  telegram__max_message_chars=100,
                                  telegram__max_backlog=3)
        self.bridge = TelegramBridge(self.config,
                                     lambda t, m: self.answered.append(t) or "ok")

    def _handle(self, *updates):
        with mock.patch("toony.bridges.telegram.call", self.api.call):
            self.bridge._handle_batch(list(updates))

    def test_an_oversized_message_gets_an_apology_not_an_answer(self):
        self._handle(message(text="x" * 500))
        self.assertEqual(self.answered, [])
        self.assertIn("too large", self.api.texts()[0])
        self.assertEqual(self.bridge.rejected, 1)

    def test_a_message_within_the_limit_is_answered(self):
        self._handle(message(text="x" * 50))
        self.assertEqual(len(self.answered), 1)

    def test_a_backlog_past_the_limit_is_apologised_for(self):
        """Messages that queued up while offline are not a conversation."""
        self._handle(*[message(text=f"message {i}", update_id=i)
                       for i in range(10)])
        self.assertEqual(len(self.answered), 3)          # the newest three
        self.assertEqual(self.answered, ["message 7", "message 8", "message 9"])
        apology = next(t for t in self.api.texts() if "did not reach" in t)
        self.assertIn("7 earlier messages", apology)

    def test_a_small_batch_is_answered_in_full(self):
        self._handle(*[message(text=f"m{i}", update_id=i) for i in range(3)])
        self.assertEqual(len(self.answered), 3)
        self.assertFalse([t for t in self.api.texts() if "did not reach" in t])

    def test_the_offset_advances_so_nothing_repeats(self):
        self._handle(message(update_id=7))
        self.assertEqual(self.bridge._offset, 8)


class TestBridgeBehaviour(unittest.TestCase):
    def test_no_token_refuses_to_start(self):
        bridge = TelegramBridge(make_config(), lambda t, m: "")
        with self.assertRaises(TelegramError):
            bridge.start()

    def test_a_failing_answer_becomes_a_message_not_a_crash(self):
        api = FakeTelegram()

        def explode(text, meta):
            raise RuntimeError("the brain caught fire")

        bridge = TelegramBridge(make_config(telegram__token="1:x",
                                            telegram__allowed_chats=["42"]),
                                explode)
        with mock.patch("toony.bridges.telegram.call", api.call):
            bridge._handle_batch([message()])
        self.assertIn("caught fire", " ".join(
            [e.get("text", "") for e in api.edited] + api.texts()))

    def test_help_is_answered_without_troubling_the_model(self):
        api = FakeTelegram()
        answered = []
        bridge = TelegramBridge(make_config(telegram__token="1:x",
                                            telegram__allowed_chats=["42"]),
                                lambda t, m: answered.append(t) or "ok")
        with mock.patch("toony.bridges.telegram.call", api.call):
            bridge._handle_batch([message(text="/help")])
        self.assertEqual(answered, [])
        self.assertIn("Toony", api.texts()[0])

    def test_a_bad_token_says_so_in_plain_words(self):
        import urllib.error

        def unauthorised(*a, **k):
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

        with mock.patch("urllib.request.urlopen", unauthorised):
            with self.assertRaises(TelegramError) as caught:
                call("bad:token", "getMe")
        self.assertIn("BotFather", str(caught.exception))

    def test_status_is_reportable_before_it_starts(self):
        status = TelegramBridge(make_config(), lambda t, m: "").status()
        self.assertFalse(status["running"])


# ------------------------------------------------------------- parallelism
class Probe:
    """A tool that records how many copies of itself run at once."""

    def __init__(self, delay=0.15):
        self.delay = delay
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def __call__(self, ctx, **kwargs):
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
        time.sleep(self.delay)
        with self._lock:
            self._live -= 1
        return "done"


class ScriptedBrain(Brain):
    def __init__(self, replies):
        self.replies = list(replies)

    def reply(self, system, messages, tools):
        return self.replies.pop(0) if self.replies else BrainReply(text="done")


class TestParallelTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = Probe()
        cls.risky = Probe()
        for index in range(4):
            REGISTRY.add(Tool(name=f"parallel_probe_{index}", description="probe",
                              risk="safe", handler=cls.probe,
                              schema={"type": "object", "properties": {},
                                      "required": []}))
        REGISTRY.add(Tool(name="serial_probe", description="probe",
                          risk="sensitive", handler=cls.risky,
                          schema={"type": "object", "properties": {},
                                  "required": []}))

    def setUp(self):
        self.probe.peak = self.risky.peak = 0

    def _run(self, names, **overrides):
        calls = [ToolCall(f"c{i}", name, {}) for i, name in enumerate(names)]
        brain = ScriptedBrain([BrainReply(tool_calls=calls),
                               BrainReply(text="finished")])
        agent = Agent(make_config(**overrides), brain)
        started = time.monotonic()
        agent.ask("go")
        return agent, time.monotonic() - started

    def test_independent_read_only_tools_overlap(self):
        agent, elapsed = self._run([f"parallel_probe_{i}" for i in range(4)])
        self.assertEqual(self.probe.peak, 4)
        self.assertLess(elapsed, 0.45)          # serial would be 0.6

    def test_results_come_back_in_the_order_they_were_asked_for(self):
        agent, _ = self._run([f"parallel_probe_{i}" for i in range(4)])
        results = [block for m in agent.history for block in m.content
                   if block.get("type") == "tool_result"]
        self.assertEqual([r["tool_use_id"] for r in results],
                         ["c0", "c1", "c2", "c3"])

    def test_turning_it_off_makes_them_serial(self):
        self._run([f"parallel_probe_{i}" for i in range(4)],
                  brain__parallel_tools=False)
        self.assertEqual(self.probe.peak, 1)

    def test_a_tool_that_asks_permission_never_runs_alongside_another(self):
        """Two spoken permission questions at once is not a conversation."""
        self._run(["serial_probe", "serial_probe", "serial_probe"],
                  tools__policy_sensitive="allow", tools__always_allow=[])
        self.assertEqual(self.risky.peak, 1)

    def test_a_single_call_is_not_sent_through_the_pool(self):
        agent, _ = self._run(["parallel_probe_0"])
        self.assertEqual(self.probe.peak, 1)


# ---------------------------------------------------------- wake word load
class TestOpenWakeWordCompatibility(unittest.TestCase):
    """0.4 takes wakeword_model_paths; 0.5 renamed it to wakeword_models."""

    def test_the_old_argument_name_is_used_when_that_is_what_exists(self):
        class Old:
            def __init__(self, wakeword_model_paths=(), inference_framework="tflite"):
                pass

        self.assertEqual(OpenWakeWordDetector._argument(Old, ["/m/hey.onnx"]),
                         {"wakeword_model_paths": ["/m/hey.onnx"]})

    def test_the_new_argument_name_is_preferred_when_available(self):
        class New:
            def __init__(self, wakeword_models=(), inference_framework="onnx"):
                pass

        self.assertEqual(OpenWakeWordDetector._argument(New, ["/m/hey.onnx"]),
                         {"wakeword_models": ["/m/hey.onnx"]})

    def test_an_unknown_signature_falls_back_to_the_current_name(self):
        class Odd:
            def __init__(self, *args, **kwargs):
                pass

        self.assertIn("wakeword_models",
                      OpenWakeWordDetector._argument(Odd, ["/m/hey.onnx"]))

    def test_the_runtime_matches_the_file(self):
        class Model:
            def __init__(self, wakeword_models=(), inference_framework="tflite"):
                pass

        self.assertEqual(OpenWakeWordDetector._framework(Model, "/m/hey.onnx"),
                         {"inference_framework": "onnx"})
        self.assertEqual(OpenWakeWordDetector._framework(Model, "/m/hey.tflite"),
                         {"inference_framework": "tflite"})

    def test_a_framework_argument_is_omitted_if_unsupported(self):
        class Model:
            def __init__(self, wakeword_models=()):
                pass

        self.assertEqual(OpenWakeWordDetector._framework(Model, "/m/hey.onnx"), {})

    def test_a_local_model_file_is_found_by_name(self):
        import tempfile

        directory = Path(tempfile.mkdtemp())
        (directory / "hey_toony.onnx").write_bytes(b"x")
        self.assertEqual(_openwakeword_paths("hey_toony", directory),
                         [str(directory / "hey_toony.onnx")])

    def test_a_missing_model_resolves_to_nothing_rather_than_guessing(self):
        self.assertEqual(_openwakeword_paths("no_such_model_xyz",
                                             Path("/nonexistent")), [])


# ----------------------------------------------------------------- barge-in
class TestBargeIn(unittest.TestCase):
    def test_it_is_off_when_configured_off(self):
        from toony.audio.bargein import build

        self.assertIsNone(build(make_config(audio__barge_in=False), lambda: None))

    def test_the_threshold_is_above_the_ordinary_one(self):
        """Otherwise it triggers on Toony's own voice through the speakers."""
        from toony.audio.bargein import build

        config = make_config(audio__energy_threshold=0.01,
                             audio__barge_in_sensitivity=2.5)
        listener = build(config, lambda: None)
        self.assertAlmostEqual(listener.threshold, 0.025)
        self.assertGreater(listener.threshold, config.get("audio.energy_threshold"))

    def test_sensitivity_below_one_does_not_lower_the_bar(self):
        from toony.audio.bargein import build

        listener = build(make_config(audio__energy_threshold=0.01,
                                     audio__barge_in_sensitivity=0.1),
                         lambda: None)
        self.assertGreaterEqual(listener.threshold, 0.01)

    def test_it_fires_once_and_only_once(self):
        from toony.audio.bargein import BargeInListener

        fired = queue.Queue()
        listener = BargeInListener(make_config(), lambda: fired.put(1))
        listener._fire(0.5)
        listener._fire(0.5)
        self.assertEqual(fired.qsize(), 1)
        self.assertTrue(listener.fired)

    def test_a_raising_handler_does_not_escape(self):
        from toony.audio.bargein import BargeInListener

        def explode():
            raise RuntimeError("no")

        BargeInListener(make_config(), explode)._fire(0.5)   # must not raise


if __name__ == "__main__":
    unittest.main()
