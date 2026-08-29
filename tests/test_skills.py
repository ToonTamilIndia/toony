"""Conversation storage, the new tools, and the KDE shortcut plumbing."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toony.agent import looks_like_refusal
from toony.brain.base import Message, ToolCall
from toony.cli import _normalise, conflicts
from toony.config import Config
from toony.history import Conversation, Store
from toony.safety import decision_for
from toony.tools import REGISTRY
from toony.tools.proc import sudo_allowed
from toony.tools.timers import parse_duration


class TestConversationStore(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()))

    def test_a_conversation_survives_a_round_trip(self):
        conversation = Conversation.new()
        conversation.messages += [Message.user_text("hello"),
                                  Message.assistant("hi there")]
        self.store.save(conversation)
        loaded = self.store.load(conversation.id)
        self.assertEqual(loaded.id, conversation.id)
        self.assertEqual(loaded.messages[1].text(), "hi there")

    def test_the_title_comes_from_the_first_thing_said(self):
        conversation = Conversation.new()
        conversation.messages.append(Message.user_text("what is the weather like"))
        self.assertEqual(conversation.display_title(), "what is the weather like")

    def test_an_explicit_title_wins(self):
        conversation = Conversation.new(title="Holiday plans")
        conversation.messages.append(Message.user_text("book a flight"))
        self.assertEqual(conversation.display_title(), "Holiday plans")

    def test_a_long_first_line_is_shortened(self):
        conversation = Conversation.new()
        conversation.messages.append(Message.user_text("word " * 40))
        self.assertLessEqual(len(conversation.display_title()), 48)

    def test_screenshots_are_not_written_to_disk(self):
        """A base64 image would be far larger than the whole conversation."""
        conversation = Conversation.new()
        conversation.messages.append(Message.user_image("look", "A" * 5000))
        self.store.save(conversation)
        raw = (self.store.dir / f"{conversation.id}.json").read_text()
        self.assertNotIn("A" * 100, raw)
        self.assertIn("screenshot", raw)

    def test_listing_is_newest_first(self):
        for name in ("first", "second", "third"):
            conversation = Conversation.new()
            conversation.messages.append(Message.user_text(name))
            self.store.save(conversation)
            time.sleep(0.01)
        titles = [row["title"] for row in self.store.list()]
        self.assertEqual(titles[0], "third")

    def test_a_recent_conversation_is_resumed(self):
        conversation = Conversation.new()
        conversation.messages.append(Message.user_text("earlier"))
        self.store.save(conversation)
        self.assertEqual(self.store.resume_or_new(120).id, conversation.id)

    def test_a_stale_conversation_starts_a_new_one(self):
        conversation = Conversation.new()
        conversation.messages.append(Message.user_text("last week"))
        conversation.updated = time.time() - 7 * 86400
        self.store.save(conversation)
        self.assertNotEqual(self.store.resume_or_new(120).id, conversation.id)

    def test_pruning_keeps_only_the_newest(self):
        store = Store(Path(tempfile.mkdtemp()), max_stored=3)
        for index in range(6):
            conversation = Conversation.new()
            conversation.messages.append(Message.user_text(f"turn {index}"))
            store.save(conversation)
            time.sleep(0.01)
        self.assertEqual(len(store.list()), 3)

    def test_a_bad_id_cannot_escape_the_directory(self):
        """Ids arrive over a socket, so they are sanitised, not trusted."""
        for attempt in ("../../etc/passwd", "/etc/shadow", "a/../../b"):
            with self.subTest(attempt=attempt):
                path = self.store._path(attempt)
                self.assertEqual(path.parent, self.store.dir)
        with self.assertRaises(ValueError):
            self.store._path("../..")      # nothing usable left after cleaning

    def test_a_corrupt_file_is_skipped_not_raised(self):
        (self.store.dir / "broken.json").write_text("{not json")
        self.assertIsNone(self.store.load("broken"))
        self.assertEqual(self.store.list(), [])

    def test_the_transcript_folds_tools_into_the_assistant_turn(self):
        conversation = Conversation.new()
        conversation.messages += [
            Message.user_text("what time is it"),
            Message.assistant("", [ToolCall("t1", "get_datetime", {})]),
            Message.tool_results([("t1", "noon", False)]),
            Message.assistant("It is noon."),
        ]
        transcript = conversation.transcript()
        self.assertEqual([entry["role"] for entry in transcript],
                         ["user", "assistant", "assistant"])
        self.assertEqual(transcript[1]["tools"], "get_datetime")
        self.assertEqual(transcript[2]["text"], "It is noon.")


class TestRefusalDetection(unittest.TestCase):
    def test_a_bare_apology_is_spotted(self):
        self.assertTrue(looks_like_refusal("I'm sorry, but I can't assist with that."))
        self.assertTrue(looks_like_refusal("Sorry, I cannot help with that."))

    def test_a_real_answer_is_not_a_refusal(self):
        self.assertFalse(looks_like_refusal("It is half past two."))
        self.assertFalse(looks_like_refusal(""))

    def test_an_apology_with_substance_is_left_alone(self):
        """A long answer that opens with an apology is still an answer."""
        text = ("I'm sorry, that did not work. " + "The disk is full and "
                "there is no space left to write the file. " * 4)
        self.assertFalse(looks_like_refusal(text))


class TestDurations(unittest.TestCase):
    def test_spoken_durations(self):
        cases = {"ten minutes": 600, "five minutes": 300, "an hour": 3600,
                 "half an hour": 1800, "a quarter of an hour": 900,
                 "1 hour 30 minutes": 5400, "90 seconds": 90,
                 "an hour and a half": 5400, "twenty five minutes": 1500}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), expected)

    def test_a_bare_number_is_minutes(self):
        self.assertEqual(parse_duration("5"), 300)

    def test_nonsense_is_zero_not_a_crash(self):
        self.assertEqual(parse_duration("tomorrow afternoon"), 0)
        self.assertEqual(parse_duration(""), 0)


class TestShortcutMatching(unittest.TestCase):
    def test_modifier_order_and_spelling_do_not_matter(self):
        self.assertEqual(_normalise("Meta+Space"), _normalise("Super+Space"))
        self.assertEqual(_normalise("Ctrl+Alt+T"), _normalise("Alt+Ctrl+T"))
        self.assertEqual(_normalise("meta+space"), _normalise("Meta+Space"))

    def test_different_keys_stay_different(self):
        self.assertNotEqual(_normalise("Meta+Space"), _normalise("Meta+Return"))

    def test_conflicts_reads_a_shortcut_file(self):
        import os
        import tempfile as tf

        directory = tf.mkdtemp()
        Path(directory, "kglobalshortcutsrc").write_text(
            "[kwin]\nSwitch Layout=Meta+Space,none,Switch keyboard layout\n"
            "[services][toony-listen.desktop]\n_launch=Meta+Space,none,Toony\n")
        previous = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = directory
        try:
            found = conflicts("Super+Space")
        finally:
            if previous is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = previous
        # KWin's binding is a clash; Toony's own entry is not.
        self.assertEqual([action for _, action in found], ["Switch Layout"])


class TestSudoGate(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def test_an_allowlisted_prefix_passes(self):
        self.assertTrue(sudo_allowed(self.config, ["journalctl", "-b", "-p", "3"]))
        self.assertTrue(sudo_allowed(self.config, ["dnf", "search", "firefox"]))

    def test_anything_else_is_refused(self):
        for argv in (["rm", "-rf", "/"], ["dnf", "remove", "kernel"],
                     ["systemctl", "poweroff"], ["journalctld"]):
            with self.subTest(argv=argv):
                self.assertFalse(sudo_allowed(self.config, argv))

    def test_a_prefix_must_match_whole_words(self):
        """'journalctl' must not authorise 'journalctl-evil'."""
        self.assertFalse(sudo_allowed(self.config, ["journalctl-evil"]))


class TestNewTools(unittest.TestCase):
    def test_the_new_skills_are_registered(self):
        names = {tool.name for tool in REGISTRY.all()}
        for expected in ("diagnose_system", "read_system_logs", "set_timer",
                         "network_status", "check_updates", "suspend",
                         "list_services", "bluetooth_status"):
            self.assertIn(expected, names)

    def test_everything_that_loses_work_is_dangerous(self):
        for name in ("power_off", "reboot", "log_out", "install_package",
                     "control_service", "hibernate"):
            self.assertEqual(REGISTRY.get(name).risk, "dangerous", name)

    def test_dangerous_tools_are_denied_by_default(self):
        config = Config()
        for name in ("power_off", "install_package", "control_service"):
            self.assertEqual(decision_for(config, REGISTRY.get(name)), "deny", name)

    def test_reading_the_system_is_allowed_without_asking(self):
        config = Config()
        for name in ("diagnose_system", "read_system_logs", "network_status"):
            self.assertEqual(decision_for(config, REGISTRY.get(name)), "allow", name)

    def test_opening_an_app_no_longer_asks(self):
        """The whole point of the always_allow list."""
        self.assertEqual(decision_for(Config(), REGISTRY.get("open_application")),
                         "allow")

    def test_every_tool_has_a_usable_schema(self):
        for tool in REGISTRY.all():
            with self.subTest(tool=tool.name):
                self.assertEqual(tool.schema["type"], "object")
                self.assertTrue(tool.description.strip())
                for required in tool.schema["required"]:
                    self.assertIn(required, tool.schema["properties"])


class TestSystemReport(unittest.TestCase):
    def test_diagnose_produces_a_readable_briefing(self):
        from toony.tools.logs import diagnose_system
        from toony.tools.registry import ToolContext

        report = diagnose_system(ToolContext(config=Config()))
        self.assertIn("UPTIME:", report)
        self.assertLess(len(report), 4000)   # it must fit in a prompt

    def test_journal_placeholders_are_not_counted_as_entries(self):
        from toony.tools.logs import _drop_placeholders

        self.assertEqual(_drop_placeholders("-- No entries --"), "")
        self.assertEqual(_drop_placeholders("real line"), "real line")


if __name__ == "__main__":
    unittest.main()
