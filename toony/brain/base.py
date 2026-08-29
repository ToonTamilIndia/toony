"""The provider-neutral shape every brain backend speaks.

The internal transcript uses Anthropic-style content blocks because they are the
richest of the three formats — text, images, tool_use and tool_result all have a
home. The OpenAI-compatible backend translates them on the way out.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal

Role = Literal["user", "assistant"]


@dataclass
class ToolSpec:
    """A tool as advertised to the model."""

    name: str
    description: str
    schema: dict[str, Any]

    def to_anthropic(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema}

    def to_openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.schema}}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    role: Role
    content: list[dict[str, Any]]

    @classmethod
    def user_text(cls, text: str) -> "Message":
        return cls("user", [{"type": "text", "text": text}])

    @classmethod
    def user_image(cls, text: str, image_b64: str,
                   media_type: str = "image/png") -> "Message":
        return cls("user", [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type,
                                         "data": image_b64}},
            {"type": "text", "text": text},
        ])

    @classmethod
    def assistant(cls, text: str, tool_calls: list[ToolCall] | None = None) -> "Message":
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for call in tool_calls or []:
            blocks.append({"type": "tool_use", "id": call.id,
                           "name": call.name, "input": call.arguments})
        return cls("assistant", blocks)

    @classmethod
    def tool_results(cls, results: list[tuple[str, str, bool]]) -> "Message":
        """results is a list of (tool_use_id, content, is_error)."""
        return cls("user", [{"type": "tool_result", "tool_use_id": tid,
                             "content": content, "is_error": is_error}
                            for tid, content, is_error in results])

    def text(self) -> str:
        return " ".join(b.get("text", "") for b in self.content
                        if b.get("type") == "text").strip()


@dataclass
class BrainReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class BrainError(RuntimeError):
    """A backend could not answer — surfaced to the user as spoken text."""


class InvalidRequest(BrainError):
    """The backend rejected the transcript itself, not the request to answer.

    Worth its own type because it is the one failure that repeats: the offending
    message is in the stored conversation, so every later turn resends it and
    fails the same way. The agent recovers by dropping the history.
    """ 


class Brain(abc.ABC):
    """One turn of conversation against some model provider."""

    name: str = "brain"

    @abc.abstractmethod
    def reply(self, system: str, messages: list[Message],
              tools: list[ToolSpec]) -> BrainReply:
        """Send the transcript and return the model's next move."""

    def stream_reply(self, system: str, messages: list[Message],
                     tools: list[ToolSpec],
                     on_text: Callable[[str], None] | None = None) -> BrainReply:
        """Streaming variant. Backends that cannot stream fall back to :meth:`reply`."""
        result = self.reply(system, messages, tools)
        if on_text and result.text:
            on_text(result.text)
        return result

    def check(self) -> str:
        """Cheap reachability probe used by ``toony doctor``. Returns a status line."""
        return "no check implemented"

    def close(self) -> None:
        pass
