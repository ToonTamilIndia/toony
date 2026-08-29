"""The control socket and the daemon's command surface, with a scripted brain."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toony import ipc
from toony.agent import Agent
from toony.app import Assistant
from toony.brain.base import Brain, BrainReply, ToolCall
from toony.config import Config


class ScriptedBrain(Brain):
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)

    def reply(self, system, messages, tools):
        return self.replies.pop(0) if self.replies else BrainReply(text="done")


class FakeVoice:
    def __init__(self):
        self.spoken = []
        self.stopped = False

    def say(self, text):
        self.spoken.append(text)

    def stop(self):
        self.stopped = True


def build_assistant(replies=None):
    """An Assistant with the audio and model stack replaced by fakes."""
    config = Config()
    assistant = Assistant.__new__(Assistant)
    assistant.config = config
    assistant.state = "idle"
    assistant.started_at = time.monotonic()
    assistant.last_error = ""
    assistant.turns = 0
    assistant.brain = ScriptedBrain(replies or [BrainReply(text="Hello there.")])
    assistant.wakeword = None
    assistant.stt = type("S", (), {"name": "local"})()
    assistant.tts = type("T", (), {"name": "piper"})()
    assistant.voice = FakeVoice()
    assistant.player = type("P", (), {"chime": lambda self, kind="start": None})()
    assistant.agent = Agent(config, assistant.brain, speak=assistant.say,
                            confirm=lambda question: False)
    assistant._running = threading.Event()
    assistant._running.set()
    assistant._turn_requested = threading.Event()
    assistant._stop_listening = threading.Event()
    assistant._turn_lock = threading.Lock()
    assistant._server = ipc.ControlServer(assistant._handle)
    return assistant


class TestControlSocket(unittest.TestCase):
    def setUp(self):
        self.assistant = build_assistant([
            BrainReply(tool_calls=[ToolCall("c1", "get_datetime", {})]),
            BrainReply(text="It is just after nine."),
        ])
        self.assistant._server.start()
        self.addCleanup(self.assistant._server.stop)

    def test_ping(self):
        reply = ipc.send("ping", timeout=5)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["state"], "idle")

    def test_status_reports_the_configured_stack(self):
        reply = ipc.send("status", timeout=5)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["brain"], "ollama:qwen3:4b")
        self.assertFalse(reply["wakeword"])

    def test_ask_runs_the_tool_loop_and_speaks(self):
        reply = ipc.send("ask", text="what time is it", speak=True, timeout=30)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["reply"], "It is just after nine.")
        for _ in range(50):
            if self.assistant.voice.spoken:
                break
            time.sleep(0.02)
        self.assertEqual(self.assistant.voice.spoken, ["It is just after nine."])

    def test_ask_without_text_is_rejected(self):
        self.assertFalse(ipc.send("ask", text="  ", timeout=5)["ok"])

    def test_listen_requests_a_turn(self):
        self.assertEqual(ipc.send("listen", timeout=5)["action"], "listening")
        self.assertTrue(self.assistant._turn_requested.is_set())

    def test_listen_while_speaking_interrupts_instead(self):
        self.assistant.state = "speaking"
        self.assertEqual(ipc.send("listen", timeout=5)["action"], "interrupted")
        self.assertTrue(self.assistant.voice.stopped)

    def test_toggle_mode_second_press_stops_listening(self):
        self.assistant.state = "listening"
        self.assertEqual(ipc.send("listen", timeout=5)["action"],
                         "stopped listening")
        self.assertTrue(self.assistant._stop_listening.is_set())

    def test_hold_mode_stops_on_release(self):
        self.assistant.config.set("ptt.mode", "hold", save=False)
        ipc.send("listen", edge="press", timeout=5)
        self.assertTrue(self.assistant._turn_requested.is_set())
        self.assertEqual(ipc.send("listen", edge="release", timeout=5)["action"],
                         "stopped listening")

    def test_reset_clears_the_conversation(self):
        ipc.send("ask", text="hello", speak=False, timeout=30)
        self.assertTrue(self.assistant.agent.history)
        self.assertTrue(ipc.send("reset", timeout=5)["ok"])
        self.assertEqual(self.assistant.agent.history, [])

    def test_unknown_command_is_an_error_not_a_crash(self):
        reply = ipc.send("frobnicate", timeout=5)
        self.assertFalse(reply["ok"])
        self.assertIn("unknown command", reply["error"])

    def test_is_running_sees_the_server(self):
        self.assertTrue(ipc.is_running())


class TestNoDaemon(unittest.TestCase):
    def test_send_reports_a_missing_daemon_instead_of_raising(self):
        reply = ipc.send("ping", timeout=2)
        self.assertFalse(reply["ok"])
        self.assertIn("not running", reply["error"].lower())

    def test_is_running_is_false(self):
        self.assertFalse(ipc.is_running())


if __name__ == "__main__":
    unittest.main(verbosity=2)
