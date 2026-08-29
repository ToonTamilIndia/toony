"""The conversation engine: transcript in, spoken answer out.

This is the piece that owns the tool-calling loop. It is deliberately free of
audio so it can be driven from the CLI (``toony ask``) and tested headlessly.
The transcript it works on belongs to a :class:`~toony.history.Conversation`,
so it is the same object the GUI lists and the same one that survives a restart.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .brain.base import Brain, BrainError, InvalidRequest, Message, ToolSpec
from .brain.prompts import build as build_prompt
from .history import Conversation, Store, store_for
from .log import get
from .safety import execute
from .tools import REGISTRY
from .tools.registry import ToolContext

log = get("agent")

# Small local models decline ordinary requests surprisingly often — "what time
# is it" is a real example. One nudged retry costs a second and fixes most of it.
_REFUSAL = re.compile(
    r"^\s*(i'?m sorry[, ]|sorry[, ]|i (can'?t|cannot|am unable to) (assist|help)"
    r"|i'?m (not able|unable) to)", re.IGNORECASE)
_NUDGE = ("That was a normal request about this computer, and you have tools for "
          "it. Answer it directly, or call the tool that reads the value. Do not "
          "decline.")


def looks_like_refusal(text: str) -> bool:
    """A bare apology with no content is a model glitch, not a real answer."""
    stripped = text.strip()
    if not stripped or len(stripped) > 200:
        return False
    return bool(_REFUSAL.match(stripped))


class Agent:
    def __init__(self, config, brain: Brain,
                 speak: Callable[[str], None] | None = None,
                 confirm: Callable[[str], bool] | None = None,
                 conversation: Conversation | None = None,
                 store: Store | None = None,
                 on_tool: Callable[[str, dict, str, bool], None] | None = None):
        self.config = config
        self.brain = brain
        self.store = store if store is not None else store_for(config)
        self.conversation = conversation or Conversation.new()
        self.on_tool = on_tool
        self.ctx = ToolContext(config=config, brain=brain, speak=speak,
                               confirm=confirm)

    # ---- the transcript ---------------------------------------------------
    @property
    def history(self) -> list[Message]:
        return self.conversation.messages

    def persist(self) -> None:
        if not self.config.get("conversation.persist", True):
            return
        try:
            self.conversation.touch()
            self.store.save(self.conversation)
        except OSError as exc:
            log.warning("could not save the conversation: %s", exc)

    def start_new(self, title: str = "") -> Conversation:
        self.conversation = Conversation.new(title)
        return self.conversation

    def open(self, conversation_id: str) -> Conversation | None:
        found = self.store.load(conversation_id)
        if found is not None:
            self.conversation = found
        return found

    def resume(self) -> Conversation:
        """Used at startup: carry on a recent thread, otherwise begin one."""
        if self.config.get("conversation.persist", True):
            window = int(self.config.get("conversation.resume_window_min", 120))
            self.conversation = self.store.resume_or_new(window)
        return self.conversation

    def reset(self) -> None:
        self.start_new()

    # ---- prompt assembly --------------------------------------------------
    def system_prompt(self) -> str:
        custom = str(self.config.get("brain.system_prompt", "") or "").strip()
        if custom:
            return custom
        extra = ""
        if self.config.get("memory.enabled", True):
            from .tools.memory import preamble
            extra = preamble()
        return build_prompt(
            name=str(self.config.get("general.name", "Toony")),
            words=int(self.config.get("general.reply_word_target", 60)),
            extra=extra,
            personality=str(self.config.get("general.personality", "friendly")),
            custom_personality=str(self.config.get("general.personality_prompt", "")),
            focus=str(self.config.get("general.focus", "general")))

    def tools(self) -> list[ToolSpec]:
        return REGISTRY.specs(self.config)

    # ---- the loop ---------------------------------------------------------
    def ask(self, text: str, on_text: Callable[[str], None] | None = None) -> str:
        """One user turn. Returns the final spoken reply."""
        if not text.strip():
            return ""
        self.history.append(Message.user_text(text.strip()))

        system = self.system_prompt()
        tools = self.tools()
        max_iterations = int(self.config.get("brain.max_tool_iterations", 6))
        stream_first = bool(self.config.get("brain.stream_from_start", True))
        started = time.monotonic()
        first_token: float | None = None
        reply = None
        self._salvaged = False

        if on_text is not None:
            inner = on_text

            def on_text(chunk: str, _inner=inner) -> None:   # noqa: F811
                nonlocal first_token
                if first_token is None and chunk.strip():
                    first_token = time.monotonic()
                _inner(chunk)

        for iteration in range(max_iterations + 1):
            last_hop = iteration == max_iterations
            hop_tools = [] if last_hop else tools
            try:
                # Stream from the very first hop. Waiting for a whole reply
                # before speaking a word of it is thirty seconds of silence on a
                # local model; any preamble before a tool call ("let me check")
                # is worth hearing anyway.
                window = self._window()
                if on_text and (iteration > 0 or stream_first):
                    reply = self.brain.stream_reply(system, window,
                                                    hop_tools, on_text)
                else:
                    reply = self.brain.reply(system, window, hop_tools)
            except InvalidRequest as exc:
                # The stored transcript contains something this backend will
                # not take. Retrying it unchanged fails forever — and it is
                # stored, so every later turn fails too. Drop back to just the
                # question and carry on.
                if self._salvage(text, exc):
                    continue
                self.persist()
                return ("Something in this conversation confused the model. "
                        "I have started a fresh one — please ask again.")
            except BrainError as exc:
                log.error("brain failed: %s", exc)
                self.persist()
                return str(exc)

            self.history.append(Message.assistant(reply.text, reply.tool_calls))

            if not reply.wants_tools:
                if self._should_retry(reply.text, iteration):
                    log.info("model declined a routine request — retrying once")
                    self.history.append(Message.user_text(_NUDGE))
                    continue
                elapsed = time.monotonic() - started
                if first_token is not None:
                    log.info("answered in %.1fs (first word after %.1fs) "
                             "after %d tool round(s)", elapsed,
                             first_token - started, iteration)
                else:
                    log.info("answered in %.1fs after %d tool round(s)",
                             elapsed, iteration)
                if on_text and iteration == 0 and reply.text:
                    on_text(reply.text)
                self.persist()
                return reply.text

            results = self._run_tools(reply.tool_calls)
            self.history.append(Message.tool_results(results))

        self.persist()
        return (reply.text if reply else "") or "I got stuck working on that."

    def _salvage(self, text: str, exc: Exception) -> bool:
        """Reduce the transcript to the current question and try once more."""
        if self._salvaged:
            log.error("the model still rejects the request: %s", exc)
            return False
        self._salvaged = True
        log.warning("the model rejected the transcript (%s) — starting a fresh "
                    "conversation and retrying", exc)
        self.persist()
        self.start_new()
        self.history.append(Message.user_text(text.strip()))
        return True

    def _should_retry(self, text: str, iteration: int) -> bool:
        return (iteration == 0
                and bool(self.config.get("brain.retry_refusals", True))
                and looks_like_refusal(text))

    # ---- running tools ----------------------------------------------------
    def _run_tools(self, calls) -> list[tuple[str, str, bool]]:
        """Run one round of tool calls, in parallel where that is safe.

        A model asking "what is the volume, the battery and the time" should not
        wait for three round trips through the desktop. But anything that has to
        ask permission must stay serial — two spoken questions at once is not a
        conversation — and so must anything that writes, because the model
        cannot tell us whether two writes touch the same file.
        """
        if len(calls) < 2 or not self.config.get("brain.parallel_tools", True):
            return [self._run_tool(call) for call in calls]

        concurrent, serial = self._partition(calls)
        if len(concurrent) < 2:
            return [self._run_tool(call) for call in calls]

        log.info("running %d tools in parallel, %d one at a time",
                 len(concurrent), len(serial))
        workers = max(1, min(int(self.config.get("tools.max_parallel", 4)),
                             len(concurrent)))
        done: dict[int, tuple[str, str, bool]] = {}
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="toony-tool") as pool:
            futures = {pool.submit(self._run_tool, call): index
                       for index, call in concurrent}
            for future, index in futures.items():
                done[index] = future.result()
        log.info("parallel tools finished in %.1fs", time.monotonic() - started)

        for index, call in serial:
            done[index] = self._run_tool(call)
        # The model matched each tool_use id to a position; keep that order.
        return [done[index] for index in range(len(calls))]

    def _partition(self, calls):
        """Split calls into (can run together, must run alone), keeping indices."""
        from .safety import decision_for

        concurrent, serial = [], []
        for index, call in enumerate(calls):
            tool = REGISTRY.resolve(call.name, self.config)
            parallel_safe = (tool is not None
                             and tool.risk == "safe"
                             and decision_for(self.config, tool) == "allow")
            (concurrent if parallel_safe else serial).append((index, call))
        return concurrent, serial

    def _run_tool(self, call) -> tuple[str, str, bool]:
        available = {t.name for t in REGISTRY.enabled(self.config)}
        tool = REGISTRY.resolve(call.name, self.config)
        if tool is None:
            log.warning("model asked for unknown tool %s", call.name)
            if REGISTRY.get(call.name) is not None:
                return call.id, (f"The {call.name} tool is not available on this "
                                 "machine right now."), True
            suggestions = ", ".join(REGISTRY.suggest(call.name, self.config))
            return call.id, (f"There is no tool called {call.name}. "
                             f"Did you mean one of: {suggestions}?"), True
        if tool.name not in available:
            return call.id, f"The {tool.name} tool is disabled.", True
        log.info("tool %s(%s)", tool.name, call.arguments)
        output, is_error = execute(tool, call.arguments, self.ctx)
        if self.on_tool:
            try:
                self.on_tool(tool.name, call.arguments, output, is_error)
            except Exception:
                log.debug("tool observer raised", exc_info=True)
        return call.id, output, is_error

    # ---- history ----------------------------------------------------------
    def _window(self) -> list[Message]:
        """What the model sees: the tail of the conversation, bounded.

        The stored conversation keeps every turn — the GUI shows all of it — so
        only the slice sent to the model is cut, and it is cut at a plain user
        message so a tool call is never separated from its result.
        """
        limit = int(self.config.get("brain.max_history_turns", 20)) * 2
        if len(self.history) <= limit:
            return list(self.history)
        cut = len(self.history) - limit
        while cut < len(self.history) and not _is_plain_user(self.history[cut]):
            cut += 1
        if cut >= len(self.history):        # nothing left to send: send it all
            return list(self.history)
        return self.history[cut:]


def _is_plain_user(message: Message) -> bool:
    return (message.role == "user"
            and all(b.get("type") != "tool_result" for b in message.content))
