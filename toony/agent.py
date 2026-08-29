"""The conversation engine: transcript in, spoken answer out.

This is the piece that owns the tool-calling loop. It is deliberately free of
audio so it can be driven from the CLI (``toony ask``) and tested headlessly.
"""

from __future__ import annotations

import time
from typing import Callable

from .brain.base import Brain, BrainError, Message, ToolSpec
from .brain.prompts import build as build_prompt
from .log import get
from .safety import execute
from .tools import REGISTRY
from .tools.registry import ToolContext

log = get("agent")


class Agent:
    def __init__(self, config, brain: Brain,
                 speak: Callable[[str], None] | None = None,
                 confirm: Callable[[str], bool] | None = None):
        self.config = config
        self.brain = brain
        self.history: list[Message] = []
        self.ctx = ToolContext(config=config, brain=brain, speak=speak,
                               confirm=confirm)

    # ---- prompt assembly --------------------------------------------------
    def system_prompt(self) -> str:
        custom = str(self.config.get("brain.system_prompt", "") or "").strip()
        if custom:
            return custom
        extra = ""
        if self.config.get("memory.enabled", True):
            from .tools.memory import preamble
            extra = preamble()
        return build_prompt(name=str(self.config.get("general.name", "Toony")),
                            words=int(self.config.get("general.reply_word_target", 60)),
                            extra=extra)

    def tools(self) -> list[ToolSpec]:
        return REGISTRY.specs(self.config)

    # ---- the loop ---------------------------------------------------------
    def ask(self, text: str, on_text: Callable[[str], None] | None = None) -> str:
        """One user turn. Returns the final spoken reply."""
        if not text.strip():
            return ""
        self.history.append(Message.user_text(text.strip()))
        self._trim()

        system = self.system_prompt()
        tools = self.tools()
        max_iterations = int(self.config.get("brain.max_tool_iterations", 6))
        started = time.monotonic()

        for iteration in range(max_iterations + 1):
            last_hop = iteration == max_iterations
            try:
                # Only stream the hop that can produce the final answer; streaming
                # a tool-call hop would speak text the user does not need.
                if on_text and iteration > 0:
                    reply = self.brain.stream_reply(system, self.history, tools, on_text)
                else:
                    reply = self.brain.reply(system, self.history,
                                             [] if last_hop else tools)
            except BrainError as exc:
                log.error("brain failed: %s", exc)
                return str(exc)

            self.history.append(Message.assistant(reply.text, reply.tool_calls))

            if not reply.wants_tools:
                elapsed = time.monotonic() - started
                log.info("answered in %.1fs after %d tool round(s)", elapsed, iteration)
                if on_text and iteration == 0 and reply.text:
                    on_text(reply.text)
                return reply.text

            results = []
            for call in reply.tool_calls:
                results.append(self._run_tool(call))
            self.history.append(Message.tool_results(results))

        return reply.text or "I got stuck working on that."

    def _run_tool(self, call) -> tuple[str, str, bool]:
        tool = REGISTRY.get(call.name)
        if tool is None:
            log.warning("model asked for unknown tool %s", call.name)
            return call.id, f"There is no tool called {call.name}.", True
        if call.name not in {t.name for t in REGISTRY.enabled(self.config)}:
            return call.id, f"The {call.name} tool is disabled.", True
        log.info("tool %s(%s)", call.name, call.arguments)
        output, is_error = execute(tool, call.arguments, self.ctx)
        return call.id, output, is_error

    # ---- history ----------------------------------------------------------
    def _trim(self) -> None:
        """Keep the transcript bounded, never splitting a tool call from its result."""
        limit = int(self.config.get("brain.max_history_turns", 20)) * 2
        if len(self.history) <= limit:
            return
        cut = len(self.history) - limit
        while cut < len(self.history) and not _is_plain_user(self.history[cut]):
            cut += 1
        self.history = self.history[cut:]

    def reset(self) -> None:
        self.history.clear()


def _is_plain_user(message: Message) -> bool:
    return (message.role == "user"
            and all(b.get("type") != "tool_result" for b in message.content))
