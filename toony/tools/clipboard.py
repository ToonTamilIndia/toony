"""Clipboard access. Wayland first, X11 as a fallback."""

from __future__ import annotations

from .proc import CommandError, run, which
from .registry import ToolContext, tool

_LIMIT = 4000


@tool(description="Read the text currently on the clipboard. Use this when the "
                  "user says 'what did I copy' or 'summarise what I copied'.",
      risk="sensitive", requires=("wl-paste|xclip",))
def read_clipboard(ctx: ToolContext) -> str:
    if which("wl-paste"):
        text = run(["wl-paste", "--no-newline"], check=False)
    else:
        text = run(["xclip", "-selection", "clipboard", "-o"], check=False)
    if not text.strip():
        return "The clipboard is empty."
    if len(text) > _LIMIT:
        return text[:_LIMIT] + f"\n[truncated, {len(text)} characters total]"
    return text


@tool(description="Put text on the clipboard so the user can paste it.",
      risk="sensitive", params={"text": {"type": "string"}}, required=["text"],
      requires=("wl-copy|xclip",))
def write_clipboard(ctx: ToolContext, text: str) -> str:
    payload = text.encode("utf-8")
    if which("wl-copy"):
        run(["wl-copy"], stdin=payload)
    else:
        run(["xclip", "-selection", "clipboard"], stdin=payload)
    words = len(text.split())
    return f"Copied {words} words to the clipboard."


@tool(description="Type text into the focused window, as if from the keyboard. "
                  "Use for dictation into another application.",
      risk="dangerous", params={"text": {"type": "string"}}, required=["text"],
      requires=("wtype|ydotool|xdotool",))
def type_text(ctx: ToolContext, text: str) -> str:
    if which("wtype"):
        run(["wtype", text])
    elif which("ydotool"):
        run(["ydotool", "type", text])
    elif which("xdotool"):
        run(["xdotool", "type", "--clearmodifiers", text])
    else:
        raise CommandError("no keyboard automation tool installed")
    return f"Typed {len(text.split())} words."
