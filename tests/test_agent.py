"""Agent loop, safety gate and config, exercised without any hardware."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toony.agent import Agent
from toony.brain.base import Brain, BrainError, BrainReply, Message, ToolCall
from toony.config import Config
from toony.safety import Denied, authorise, execute
from toony.tools import REGISTRY
from toony.tools.registry import ToolContext


class ScriptedBrain(Brain):
    """Replays a fixed list of replies and records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def reply(self, system, messages, tools):
        self.calls.append({"system": system, "messages": list(messages),
                           "tools": [t.name for t in tools]})
        if not self.replies:
            return BrainReply(text="(no more scripted replies)")
        return self.replies.pop(0)


def make_config(**overrides):
    cfg = Config()
    for key, value in overrides.items():
        cfg.set(key.replace("__", "."), value, save=False)
    return cfg


class TestAgentLoop(unittest.TestCase):
    def test_plain_answer(self):
        brain = ScriptedBrain([BrainReply(text="It is sunny.")])
        agent = Agent(make_config(), brain)
        self.assertEqual(agent.ask("what's the weather"), "It is sunny.")
        self.assertEqual(len(agent.history), 2)

    def test_tool_round_trip(self):
        brain = ScriptedBrain([
            BrainReply(tool_calls=[ToolCall("t1", "get_datetime", {})]),
            BrainReply(text="It is just after nine."),
        ])
        agent = Agent(make_config(), brain)
        self.assertEqual(agent.ask("what time is it"), "It is just after nine.")
        # user, assistant(tool_use), user(tool_result), assistant(text)
        self.assertEqual([m.role for m in agent.history],
                         ["user", "assistant", "user", "assistant"])
        result_block = agent.history[2].content[0]
        self.assertEqual(result_block["type"], "tool_result")
        self.assertFalse(result_block["is_error"])
        self.assertIn("It is", result_block["content"])

    def test_unknown_tool_is_reported_not_raised(self):
        brain = ScriptedBrain([
            BrainReply(tool_calls=[ToolCall("t1", "make_coffee", {})]),
            BrainReply(text="I cannot do that."),
        ])
        agent = Agent(make_config(), brain)
        agent.ask("coffee please")
        self.assertTrue(agent.history[2].content[0]["is_error"])

    def test_final_hop_drops_tools_to_force_an_answer(self):
        loop = [BrainReply(tool_calls=[ToolCall(f"t{i}", "get_datetime", {})])
                for i in range(9)]
        brain = ScriptedBrain(loop)
        agent = Agent(make_config(brain__max_tool_iterations=3), brain)
        agent.ask("loop forever")
        self.assertEqual(brain.calls[-1]["tools"], [])
        self.assertEqual(len(brain.calls), 4)

    def test_brain_error_becomes_a_spoken_sentence(self):
        class Broken(Brain):
            def reply(self, system, messages, tools):
                raise BrainError("I could not reach the Claude API.")

        agent = Agent(make_config(), Broken())
        self.assertEqual(agent.ask("hello"), "I could not reach the Claude API.")

    def test_history_trim_keeps_tool_pairs_together(self):
        agent = Agent(make_config(brain__max_history_turns=2), ScriptedBrain([]))
        agent.history = [
            Message.user_text("one"),
            Message.assistant("", [ToolCall("a", "get_datetime", {})]),
            Message.tool_results([("a", "noon", False)]),
            Message.assistant("It is noon."),
            Message.user_text("two"),
            Message.assistant("ok"),
        ]
        agent._trim()
        self.assertTrue(agent.history[0].role == "user")
        self.assertNotEqual(agent.history[0].content[0].get("type"), "tool_result")


class TestSafety(unittest.TestCase):
    def setUp(self):
        self.tool = REGISTRY.get("open_application")

    def test_deny_policy_blocks(self):
        ctx = ToolContext(config=make_config(tools__policy_sensitive="deny"))
        with self.assertRaises(Denied):
            authorise(self.tool, {"name": "firefox"}, ctx)

    def test_ask_policy_respects_refusal(self):
        ctx = ToolContext(config=make_config(tools__policy_sensitive="ask"),
                          confirm=lambda question: False)
        text, is_error = execute(self.tool, {"name": "firefox"}, ctx)
        self.assertTrue(is_error)
        self.assertIn("declined", text)

    def test_ask_policy_passes_a_readable_question(self):
        asked = []
        ctx = ToolContext(config=make_config(tools__policy_sensitive="ask"),
                          confirm=lambda q: asked.append(q) or False)
        execute(self.tool, {"name": "firefox"}, ctx)
        self.assertEqual(asked, ["Can I open application with name firefox?"])

    def test_tool_exception_is_contained(self):
        ctx = ToolContext(config=make_config(tools__policy_sensitive="allow"))
        text, is_error = execute(REGISTRY.get("read_text_file"),
                                 {"path": "/etc/shadow"}, ctx)
        self.assertTrue(is_error)  # outside $HOME
        self.assertIn("home directory", text)


class TestToolPlumbing(unittest.TestCase):
    def test_shell_tool_hidden_until_enabled(self):
        cfg = make_config()
        names = {t.name for t in REGISTRY.enabled(cfg)}
        self.assertNotIn("run_command", names)
        cfg.set("tools.shell.enabled", True, save=False)
        self.assertIn("run_command", {t.name for t in REGISTRY.enabled(cfg)})

    def test_shell_allowlist_and_metacharacters(self):
        cfg = make_config(tools__shell__enabled=True)
        ctx = ToolContext(config=cfg)
        tool = REGISTRY.get("run_command")
        self.assertIn("not on my allowlist", tool.call(ctx, {"command": "rm -rf /"}))
        self.assertIn("will not run", tool.call(ctx, {"command": "ls | mail me"}))
        self.assertIn("allowlist", tool.call(ctx, {"command": "systemctl restart x"}))

    def test_extra_arguments_from_the_model_are_dropped(self):
        ctx = ToolContext(config=make_config())
        out = REGISTRY.get("get_datetime").call(ctx, {"timezone": "UTC", "x": 1})
        self.assertIn("It is", out)

    def test_disabled_list_hides_a_tool(self):
        cfg = make_config(tools__disabled=["get_datetime"])
        self.assertNotIn("get_datetime", {t.name for t in REGISTRY.enabled(cfg)})


class TestConfig(unittest.TestCase):
    def test_types_are_preserved_through_string_input(self):
        cfg = Config()
        self.assertIs(cfg.set("wakeword.enabled", "true", save=False), True)
        self.assertEqual(cfg.set("audio.silence_ms", "500", save=False), 500)
        self.assertEqual(cfg.set("tts.speed", "1.25", save=False), 1.25)
        self.assertEqual(cfg.set("tools.disabled", "a, b", save=False), ["a", "b"])

    def test_api_key_falls_back_to_the_environment(self):
        import os
        cfg = Config()
        os.environ["TOONY_TEST_KEY"] = "sk-test"
        cfg.set("brain.claude.api_key_env", "TOONY_TEST_KEY", save=False)
        self.assertEqual(cfg.api_key("brain.claude"), "sk-test")
        cfg.set("brain.claude.api_key", "explicit", save=False)
        self.assertEqual(cfg.api_key("brain.claude"), "explicit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
