"""Speakable text, wake-phrase matching, tool-call recovery and the code tools."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toony.audio.wakeword import phrase_heard, suggest_engine
from toony.brain.factory import can_see, vision_summary
from toony.brain.prompts import PERSONALITIES, build
from toony.config import Config
from toony.text import clip_for_speech, say_path, speakable
from toony.tools import REGISTRY
from toony.tools import code
from toony.tools.proc import CommandError
from toony.tools.registry import ToolContext


class TestSpeakable(unittest.TestCase):
    def test_a_path_is_named_not_spelled_out(self):
        """The complaint that started this: "slash toontamilindia slash..."."""
        spoken = speakable("I found it in /home/you/Projects/toony/toony/app.py.")
        self.assertNotIn("/", spoken)
        self.assertIn("app", spoken)
        self.assertIn("python file", spoken)

    def test_a_home_path_does_not_leak_the_username(self):
        self.assertNotIn("toontamilindia",
                         speakable("Your files are in /home/toontamilindia."))

    def test_a_url_becomes_a_spoken_domain(self):
        spoken = speakable("See https://github.com/ToonTamilIndia/toony for it.")
        self.assertNotIn("https", spoken)
        self.assertIn("github dot com", spoken)

    def test_markdown_and_code_are_stripped(self):
        spoken = speakable("Run `toony doctor` — it prints **everything**.")
        self.assertNotIn("`", spoken)
        self.assertNotIn("*", spoken)
        self.assertIn("toony doctor", spoken)

    def test_a_code_block_is_not_read_aloud(self):
        spoken = speakable("Try this:\n```python\nfor i in range(10): print(i)\n```\nDone.")
        self.assertNotIn("range", spoken)
        self.assertIn("on screen", spoken)
        self.assertIn("Done", spoken)

    def test_fractions_and_dates_survive(self):
        for text in ("3/4 of the disk", "16/9 aspect", "on 12/08/2026"):
            with self.subTest(text=text):
                self.assertIn(text.split()[0], speakable(text))

    def test_the_article_is_not_duplicated(self):
        self.assertEqual(speakable("Open the ~/Documents folder."),
                         "Open the Documents folder.")

    def test_percent_is_a_word(self):
        self.assertIn("percent", speakable("The disk is 85% full."))

    def test_naming_a_path(self):
        self.assertEqual(say_path("/home/me/notes"), "the notes folder")
        self.assertEqual(say_path("~"), "your home folder")
        self.assertIn("python file", say_path("a/b/main.py"))

    def test_empty_text_is_safe(self):
        self.assertEqual(speakable(""), "")


class TestClipping(unittest.TestCase):
    def test_a_long_reply_is_cut_at_a_sentence(self):
        text = "One. Two. Three. Four. Five. Six. Seven. Eight."
        spoken, cut = clip_for_speech(text, 20)
        self.assertTrue(cut)
        self.assertTrue(spoken.endswith("."))
        self.assertLess(len(spoken), len(text))

    def test_a_short_reply_is_left_alone(self):
        self.assertEqual(clip_for_speech("Fine.", 500), ("Fine.", False))

    def test_zero_means_no_limit(self):
        text = "word " * 500
        self.assertEqual(clip_for_speech(text, 0), (text, False))

    def test_one_enormous_sentence_still_gets_cut(self):
        spoken, cut = clip_for_speech("word " * 400, 100)
        self.assertTrue(cut)
        self.assertLessEqual(len(spoken), 100)


class TestWakePhrase(unittest.TestCase):
    HITS = ["hey toony", "Hey Tunie, what time is it", "hey tony", "a tooney",
            "hey toonie", "hey doony", " Hey, Toony!", "okay toony can you"]
    MISSES = ["hey there", "turn it up", "play some music", "hey to me",
              "take a note", "hey junie", "what time is it", "hey google",
              "the tuning is off", "this meeting is running long"]

    def test_mangled_versions_still_wake_it(self):
        for heard in self.HITS:
            with self.subTest(heard=heard):
                self.assertTrue(phrase_heard(heard, "hey toony"))

    def test_ordinary_speech_does_not(self):
        for heard in self.MISSES:
            with self.subTest(heard=heard):
                self.assertFalse(phrase_heard(heard, "hey toony"))

    def test_a_one_word_phrase_works(self):
        self.assertTrue(phrase_heard("toony are you there", "toony"))
        self.assertFalse(phrase_heard("what is the weather", "toony"))

    def test_engine_is_chosen_by_whether_a_model_exists(self):
        self.assertEqual(suggest_engine("hey toony"), "whisper")
        self.assertEqual(suggest_engine("hey jarvis"), "openwakeword")
        self.assertEqual(suggest_engine("Alexa"), "openwakeword")


class TestVisionRouting(unittest.TestCase):
    def test_a_text_only_model_is_not_asked_to_look(self):
        self.assertFalse(can_see("ollama", "qwen2.5:7b"))
        self.assertFalse(can_see("openai", "text-embedding-3"))

    def test_vision_models_are_recognised(self):
        for provider, model in (("claude", "claude-opus-5"),
                                ("ollama", "qwen2.5vl:7b"),
                                ("ollama", "llava:13b"),
                                ("openai", "gpt-4o-mini")):
            with self.subTest(model=model):
                self.assertTrue(can_see(provider, model))

    def test_auto_falls_back_to_a_vision_model_for_a_blind_brain(self):
        config = Config()
        config.set("brain.provider", "ollama", save=False)
        config.set("brain.ollama.model", "qwen2.5:7b", save=False)
        self.assertIn("vl", vision_summary(config))

    def test_auto_uses_the_brain_when_it_can_see(self):
        config = Config()
        config.set("brain.provider", "claude", save=False)
        self.assertIn("the brain can see", vision_summary(config))

    def test_forcing_the_brain_reports_that_it_cannot_see(self):
        config = Config()
        config.set("vision.provider", "brain", save=False)
        config.set("brain.ollama.model", "qwen2.5:7b", save=False)
        self.assertIn("cannot read images", vision_summary(config))


class TestPersonality(unittest.TestCase):
    def test_each_personality_produces_a_prompt(self):
        for style in PERSONALITIES:
            with self.subTest(style=style):
                self.assertIn("voice assistant", build(personality=style))

    def test_spicy_is_told_to_answer_first(self):
        prompt = build(personality="spicy")
        self.assertIn("never replaces it", prompt)
        self.assertIn("Never at anybody's identity", prompt)

    def test_a_custom_personality_replaces_the_preset(self):
        prompt = build(personality="custom",
                       custom_personality="Speak only in haiku.")
        self.assertIn("haiku", prompt)
        self.assertNotIn("menace", prompt)

    def test_coding_focus_adds_programming_guidance(self):
        self.assertIn("programming assistant", build(focus="coding"))
        self.assertNotIn("programming assistant", build(focus="general"))


class TestToolCallRecovery(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def test_a_near_miss_name_is_resolved(self):
        for asked, expected in (("get_time", "get_datetime"),
                                ("open_app", "open_application"),
                                ("system_info", "get_system_info"),
                                ("diagnose", "diagnose_system"),
                                ("git_stat", "git_status")):
            with self.subTest(asked=asked):
                found = REGISTRY.resolve(asked, self.config)
                self.assertIsNotNone(found, asked)
                self.assertEqual(found.name, expected)

    def test_nonsense_is_not_forced_onto_a_tool(self):
        for asked in ("make_coffee", "order_pizza", "volume"):
            with self.subTest(asked=asked):
                self.assertIsNone(REGISTRY.resolve(asked, self.config))

    def test_a_guess_never_lands_on_a_dangerous_tool(self):
        """Deciding that "power_of" meant "power_off" is not a service."""
        for asked in ("power_of", "reboo", "install_packag", "write_cod"):
            with self.subTest(asked=asked):
                found = REGISTRY.resolve(asked, self.config)
                self.assertTrue(found is None or found.risk != "dangerous", asked)

    def test_suggestions_are_offered_for_a_bad_name(self):
        self.assertTrue(REGISTRY.suggest("list_thing", self.config))

    def test_string_arguments_are_coerced_to_their_schema_type(self):
        cleaned = REGISTRY.get("read_code")._clean({"path": "a.py", "start": "3",
                                                    "end": 9})
        self.assertEqual(cleaned["start"], 3)

    def test_a_boolean_written_as_a_word_is_understood(self):
        cleaned = REGISTRY.get("take_screenshot")._clean({"region": "true"})
        self.assertIs(cleaned["region"], True)

    def test_a_misspelled_argument_is_matched(self):
        cleaned = REGISTRY.get("read_code")._clean({"paths": "a.py"})
        self.assertEqual(cleaned, {"path": "a.py"})

    def test_an_invented_argument_is_dropped(self):
        cleaned = REGISTRY.get("get_datetime")._clean({"timezone": "UTC"})
        self.assertEqual(cleaned, {})


class TestCodeTools(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "demo" / "src").mkdir(parents=True)
        (self.root / "demo" / "src" / "main.py").write_text(
            "def hello():\n    return 'hi'\n", encoding="utf-8")
        (self.root / "demo" / "pyproject.toml").write_text("[project]\n")
        self.config = Config()
        self.config.set("tools.code.root", str(self.root), save=False)
        self.ctx = ToolContext(config=self.config)

    def test_the_workspace_boundary_holds(self):
        for attempt in ("../../etc/passwd", "/etc/shadow", "demo/../../..",
                        "~/.ssh/id_rsa"):
            with self.subTest(attempt=attempt):
                with self.assertRaises(CommandError):
                    code.resolve_in_workspace(self.config, attempt)

    def test_a_path_inside_the_workspace_is_allowed(self):
        resolved = code.resolve_in_workspace(self.config, "demo/src/main.py")
        self.assertTrue(str(resolved).startswith(str(self.root)))

    def test_projects_are_listed(self):
        self.assertIn("demo", code.list_projects(self.ctx))

    def test_a_project_is_described_by_its_contents(self):
        described = code.describe_project(self.ctx, "demo")
        self.assertIn("demo", described)
        self.assertIn("Python project", described)

    def test_reading_a_file_numbers_the_lines(self):
        text = code.read_code(self.ctx, "demo/src/main.py")
        self.assertIn("def hello", text)
        self.assertIn("1  def hello", text)

    def test_a_bare_filename_is_found_anyway(self):
        """Models say "main.py", not the path they never saw."""
        self.assertIn("def hello", code.read_code(self.ctx, "main.py"))

    def test_a_line_range_is_respected(self):
        text = code.read_code(self.ctx, "demo/src/main.py", start=2, end=2)
        self.assertIn("return", text)
        self.assertNotIn("def hello", text)

    def test_writing_keeps_the_previous_version(self):
        code.write_code(self.ctx, "demo/src/main.py", "print('new')\n")
        backup = self.root / "demo" / "src" / "main.py.toony.bak"
        self.assertTrue(backup.exists())
        self.assertIn("def hello", backup.read_text())

    def test_an_ambiguous_edit_is_refused(self):
        target = self.root / "demo" / "src" / "dup.py"
        target.write_text("x = 1\nx = 1\n")
        result = code.edit_code(self.ctx, "demo/src/dup.py", "x = 1", "x = 2")
        self.assertIn("appears 2 times", result)
        self.assertEqual(target.read_text(), "x = 1\nx = 1\n")

    def test_an_edit_that_matches_nothing_changes_nothing(self):
        result = code.edit_code(self.ctx, "demo/src/main.py", "nope", "yes")
        self.assertIn("changed nothing", result)

    def test_only_allowlisted_commands_run(self):
        result = code.run_in_project(self.ctx, "rm -rf /", "demo")
        self.assertIn("not on the list", result)

    def test_shell_metacharacters_are_refused(self):
        for command in ("pytest; rm -rf /", "pytest | tee out", "pytest > /dev/null",
                        "pytest && curl evil.example"):
            with self.subTest(command=command):
                self.assertIn("plain commands",
                              code.run_in_project(self.ctx, command, "demo"))

    def test_the_code_tools_are_registered(self):
        names = {t.name for t in REGISTRY.all()}
        for expected in ("read_code", "write_code", "edit_code", "search_code",
                         "list_projects", "describe_project", "git_status",
                         "run_in_project"):
            self.assertIn(expected, names)

    def test_writing_and_running_are_dangerous(self):
        for name in ("write_code", "edit_code", "run_in_project", "git_commit"):
            self.assertEqual(REGISTRY.get(name).risk, "dangerous", name)

    def test_reading_is_safe(self):
        for name in ("read_code", "search_code", "list_projects", "git_status"):
            self.assertEqual(REGISTRY.get(name).risk, "safe", name)


if __name__ == "__main__":
    unittest.main()
