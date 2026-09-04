"""The control socket and the daemon's command surface, with a scripted brain."""

from __future__ import annotations

import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toony import ipc
from toony.agent import Agent
from toony.app import Assistant
from toony.audio.capture import MicrophonePool
from toony.automation import Scheduler, Watcher
from toony.brain.ollama import WarmKeeper
from toony.brain.base import Brain, BrainReply, ToolCall
from toony.config import Config
from toony.history import Store


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


def build_assistant(replies=None, store=None):
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
    assistant.store = store or Store(Path(tempfile.mkdtemp()))
    assistant.agent = Agent(config, assistant.brain, speak=assistant.say,
                            confirm=lambda question: False,
                            store=assistant.store, on_tool=assistant._on_tool)
    assistant._running = threading.Event()
    assistant._running.set()
    assistant._turn_requested = threading.Event()
    assistant._stop_listening = threading.Event()
    assistant._turn_lock = threading.Lock()
    assistant._pending = {}
    assistant._interrupted = False
    assistant._requested_at = 0.0
    assistant._from_barge_in = False
    assistant._last_tap = 0.0
    assistant._key_down = False
    assistant.telegram = None
    assistant._chat_conversations = {}
    assistant.mics = MicrophonePool(config)
    assistant.hotkey = None
    assistant.warmer = WarmKeeper(config)
    assistant.routines = Scheduler(config, assistant._run_routine)
    assistant.watcher = Watcher(config, assistant.routines.fire)
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
        self.assertEqual(reply["brain_configured"], "ollama:qwen2.5:7b")
        self.assertFalse(reply["wakeword"])
        # No routing block when there is only one backend to talk to.
        self.assertIsNone(reply["routing"])
        self.assertEqual(reply["ptt"]["mode"], "toggle")

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

    def test_reset_starts_a_new_conversation(self):
        ipc.send("ask", text="hello", speak=False, timeout=30)
        self.assertTrue(self.assistant.agent.history)
        before = self.assistant.agent.conversation.id
        self.assertTrue(ipc.send("reset", timeout=5)["ok"])
        self.assertEqual(self.assistant.agent.history, [])
        self.assertNotEqual(self.assistant.agent.conversation.id, before)


class TestConversations(unittest.TestCase):
    def setUp(self):
        self.assistant = build_assistant([BrainReply(text="Understood.")] * 6)
        self.assistant._server.start()
        self.addCleanup(self.assistant._server.stop)

    def test_a_turn_is_saved_and_can_be_listed(self):
        ipc.send("ask", text="remember the milk", speak=False, timeout=30)
        rows = ipc.send("conversations", timeout=10)["conversations"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "remember the milk")
        self.assertEqual(rows[0]["turns"], 1)

    def test_an_old_conversation_can_be_reopened(self):
        ipc.send("ask", text="first thread", speak=False, timeout=30)
        first = ipc.send("status", timeout=5)["conversation"]
        ipc.send("conversation", action="new", timeout=10)
        ipc.send("ask", text="second thread", speak=False, timeout=30)
        self.assertNotEqual(ipc.send("status", timeout=5)["conversation"], first)

        reply = ipc.send("conversation", action="open", id=first, timeout=10)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["id"], first)
        self.assertEqual(reply["transcript"][0]["text"], "first thread")
        # And the assistant is genuinely back in it, not just showing it.
        self.assertEqual(ipc.send("status", timeout=5)["conversation"], first)

    def test_deleting_a_conversation_removes_it(self):
        ipc.send("ask", text="throwaway", speak=False, timeout=30)
        target = ipc.send("status", timeout=5)["conversation"]
        self.assertTrue(ipc.send("conversation", action="delete", id=target,
                                 timeout=10)["ok"])
        rows = ipc.send("conversations", timeout=10)["conversations"]
        self.assertNotIn(target, [r["id"] for r in rows])

    def test_asking_in_a_named_conversation_continues_it(self):
        ipc.send("ask", text="one", speak=False, timeout=30)
        target = ipc.send("status", timeout=5)["conversation"]
        ipc.send("conversation", action="new", timeout=10)
        ipc.send("ask", text="two", speak=False, conversation=target, timeout=30)
        transcript = ipc.send("transcript", id=target, timeout=10)["transcript"]
        self.assertEqual([t["text"] for t in transcript if t["role"] == "user"],
                         ["one", "two"])

    def test_unknown_conversation_is_an_error(self):
        reply = ipc.send("conversation", action="open", id="nope", timeout=10)
        self.assertFalse(reply["ok"])


class TestEventStream(unittest.TestCase):
    def setUp(self):
        self.assistant = build_assistant([BrainReply(text="All done.")])
        self.assistant._server.start()
        self.addCleanup(self.assistant._server.stop)

    def _collect(self, seconds=6.0):
        """Subscribe on a background thread and gather what arrives."""
        events: queue.Queue = queue.Queue()

        def reader():
            try:
                for event in ipc.subscribe():
                    events.put(event)
            except OSError:
                pass

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2.0
        while self.assistant._server.subscribers == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        return events

    def test_a_turn_is_published_to_a_subscriber(self):
        events = self._collect()
        self.assertEqual(self.assistant._server.subscribers, 1)
        ipc.send("ask", text="hello there", speak=False, timeout=30)

        seen = {}
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and "reply" not in seen:
            try:
                event = events.get(timeout=0.5)
            except queue.Empty:
                continue
            seen.setdefault(str(event.get("event")), event)
        self.assertEqual(seen["heard"]["text"], "hello there")
        self.assertEqual(seen["reply"]["text"], "All done.")

    def test_a_window_answers_the_permission_question(self):
        """With a window attached, the yes/no comes from a click, not the mic."""
        self._collect()
        answers = queue.Queue()

        def respond():
            for event in ipc.subscribe():
                if event.get("event") == "confirm":
                    ipc.send("confirm", id=event["id"], allow=True, timeout=5)
                    answers.put(event["question"])
                    return

        threading.Thread(target=respond, daemon=True).start()
        deadline = time.monotonic() + 2.0
        while self.assistant._server.subscribers < 2 and time.monotonic() < deadline:
            time.sleep(0.02)

        allowed = self.assistant._confirm("Can I open the door?")
        self.assertTrue(allowed)
        self.assertEqual(answers.get(timeout=2), "Can I open the door?")

    def test_the_activation_token_reaches_the_window(self):
        """Wayland refuses a client's own focus request; only a token gets in."""
        events = self._collect()
        ipc.send("listen", activation_token="kwin-token-abc", timeout=5)

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if event.get("event") == "listen_requested":
                self.assertEqual(event["activation_token"], "kwin-token-abc")
                return
        self.fail("no listen_requested event arrived")

    def test_a_hotkey_press_without_a_token_still_works(self):
        events = self._collect()
        self.assertTrue(ipc.send("listen", timeout=5)["ok"])
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if event.get("event") == "listen_requested":
                self.assertEqual(event["activation_token"], "")
                return
        self.fail("no listen_requested event arrived")

    def test_no_window_answer_falls_back_rather_than_hanging(self):
        self._collect()
        self.assistant.config.set("tools.confirm_timeout_s", 0.4, save=False)
        # No responder, and no microphone either: it must return, not block.
        self.assertFalse(self.assistant._confirm("Can I delete everything?"))

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
