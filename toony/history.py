"""Conversations that survive a restart.

The daemon used to hold one transcript in memory, so every restart — and every
``toony ask`` that fell back to running locally — began from nothing. Here a
conversation is a file: it can be listed, reopened, renamed and deleted, which
is what the GUI's sidebar is built on.

Screenshots are dropped on the way to disk. A conversation is a record of what
was said, not a photo album, and base64 images would dwarf everything else.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from .brain.base import Message
from .log import get
from .paths import CONVERSATION_DIR

log = get("history")

_IMAGE_PLACEHOLDER = {"type": "text", "text": "[a screenshot was shared here]"}


def _now() -> float:
    return time.time()


@dataclass
class Conversation:
    id: str
    title: str = ""
    created: float = field(default_factory=_now)
    updated: float = field(default_factory=_now)
    messages: list[Message] = field(default_factory=list)

    @classmethod
    def new(cls, title: str = "") -> "Conversation":
        return cls(id=uuid.uuid4().hex[:12], title=title)

    # ---- naming -----------------------------------------------------------
    def display_title(self) -> str:
        """A title for the sidebar, derived from the first thing that was said."""
        if self.title:
            return self.title
        for message in self.messages:
            if message.role == "user":
                text = message.text().strip()
                if text:
                    return _shorten(text)
        return "New conversation"

    def preview(self) -> str:
        for message in reversed(self.messages):
            text = message.text().strip()
            if text:
                return _shorten(text, 90)
        return ""

    @property
    def turns(self) -> int:
        return sum(1 for m in self.messages if _is_plain_user(m))

    def touch(self) -> None:
        self.updated = _now()

    # ---- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "messages": [{"role": m.role, "content": _strip_images(m.content)}
                         for m in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Conversation":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            title=str(data.get("title", "")),
            created=float(data.get("created", _now())),
            updated=float(data.get("updated", _now())),
            messages=[Message(m.get("role", "user"), list(m.get("content", [])))
                      for m in data.get("messages", [])],
        )

    def summary(self) -> dict[str, Any]:
        """The cheap shape the GUI's sidebar and ``toony conversations`` list."""
        return {"id": self.id, "title": self.display_title(),
                "created": self.created, "updated": self.updated,
                "turns": self.turns, "preview": self.preview()}

    def transcript(self) -> list[dict[str, str]]:
        """Flatten to speaker/text pairs for display. Tool traffic is folded in."""
        out: list[dict[str, str]] = []
        for message in self.messages:
            text = message.text().strip()
            tools = [b.get("name", "") for b in message.content
                     if b.get("type") == "tool_use"]
            if _is_plain_user(message):
                if text:
                    out.append({"role": "user", "text": text})
            elif message.role == "assistant":
                if text or tools:
                    out.append({"role": "assistant", "text": text,
                                "tools": ", ".join(t for t in tools if t)})
        return out


def _shorten(text: str, limit: int = 48) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _is_plain_user(message: Message) -> bool:
    return (message.role == "user"
            and all(b.get("type") != "tool_result" for b in message.content))


def _strip_images(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_IMAGE_PLACEHOLDER if b.get("type") == "image" else b for b in content]


class Store:
    """Conversations on disk, one JSON file each."""

    def __init__(self, directory=None, max_stored: int = 100):
        self.dir = directory or CONVERSATION_DIR
        self.max_stored = max_stored

    def _path(self, conversation_id: str):
        # Ids are generated here, but never trust one that arrives over the socket.
        safe = re.sub(r"[^A-Za-z0-9_-]", "", conversation_id)[:64]
        if not safe:
            raise ValueError("bad conversation id")
        return self.dir / f"{safe}.json"

    def save(self, conversation: Conversation) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(conversation.id)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(conversation.to_dict(), ensure_ascii=False),
                        encoding="utf-8")
        os.replace(temp, path)  # never leave a half-written conversation behind
        self.prune()

    def load(self, conversation_id: str) -> Conversation | None:
        path = self._path(conversation_id)
        if not path.exists():
            return None
        try:
            return Conversation.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            log.warning("unreadable conversation %s: %s", conversation_id, exc)
            return None

    def delete(self, conversation_id: str) -> bool:
        try:
            self._path(conversation_id).unlink()
            return True
        except (OSError, ValueError):
            return False

    def _files(self) -> list:
        if not self.dir.is_dir():
            return []
        return sorted(self.dir.glob("*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for path in self._files()[:limit]:
            conversation = self.load(path.stem)
            if conversation:
                out.append(conversation.summary())
        return out

    def latest(self) -> Conversation | None:
        for path in self._files():
            conversation = self.load(path.stem)
            if conversation:
                return conversation
        return None

    def resume_or_new(self, window_minutes: int = 120) -> Conversation:
        """Reopen the last conversation if it is recent, else start a fresh one.

        Picking up a two-day-old thread would confuse the model far more than it
        would help the user, so age decides.
        """
        recent = self.latest()
        if recent and recent.messages:
            if _now() - recent.updated <= window_minutes * 60:
                return recent
        return Conversation.new()

    def prune(self) -> None:
        for path in self._files()[self.max_stored:]:
            try:
                path.unlink()
            except OSError:
                pass


def store_for(config) -> Store:
    return Store(max_stored=int(config.get("conversation.max_stored", 100)))
