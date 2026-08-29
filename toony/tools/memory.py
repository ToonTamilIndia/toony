"""Durable facts the user asks Toony to remember."""

from __future__ import annotations

import json
import time
from typing import Any

from ..paths import MEMORY_FILE
from .registry import ToolContext, tool


def _load() -> list[dict[str, Any]]:
    try:
        with open(MEMORY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(facts: list[dict[str, Any]]) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEMORY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(facts, indent=1), encoding="utf-8")
    tmp.replace(MEMORY_FILE)


def preamble(limit: int = 40) -> str:
    """Remembered facts, formatted for the system prompt."""
    facts = _load()[-limit:]
    if not facts:
        return ""
    lines = "\n".join(f"- {f['text']}" for f in facts)
    return f"Things you have been asked to remember about this user:\n{lines}"


@tool(description="Remember a fact about the user or their machine for future "
                  "conversations. Use only when the user asks you to remember.",
      params={"fact": {"type": "string",
                       "description": "The fact, written as a short sentence."}},
      required=["fact"])
def remember(ctx: ToolContext, fact: str) -> str:
    fact = fact.strip()
    if not fact:
        return "There was nothing to remember."
    facts = _load()
    if any(f["text"].lower() == fact.lower() for f in facts):
        return "I already knew that."
    facts.append({"text": fact, "at": time.strftime("%Y-%m-%d %H:%M")})
    limit = int(ctx.config.get("memory.max_facts", 200)) if ctx.config else 200
    _save(facts[-limit:])
    return "Remembered."


@tool(description="Look through remembered facts.",
      params={"query": {"type": "string", "description": "Optional filter word."}})
def recall(ctx: ToolContext, query: str = "") -> str:
    facts = _load()
    if query:
        facts = [f for f in facts if query.lower() in f["text"].lower()]
    if not facts:
        return "I have not been asked to remember anything about that."
    return "\n".join(f"- {f['text']} (noted {f['at']})" for f in facts[-20:])


@tool(description="Forget a remembered fact.", risk="sensitive",
      params={"query": {"type": "string",
                        "description": "Words identifying the fact to drop."}},
      required=["query"])
def forget(ctx: ToolContext, query: str) -> str:
    facts = _load()
    keep = [f for f in facts if query.lower() not in f["text"].lower()]
    dropped = len(facts) - len(keep)
    if not dropped:
        return "I found nothing matching that to forget."
    _save(keep)
    return f"Forgot {dropped} thing{'s' if dropped > 1 else ''}."
