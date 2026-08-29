"""Screenshots, and asking the model about what is on screen."""

from __future__ import annotations

import base64
import time
from pathlib import Path

from ..log import get
from ..paths import SCREENSHOT_DIR
from .proc import CommandError, run, which
from .registry import ToolContext, tool

log = get("tools.screen")

# Ordered by preference. KDE ships spectacle; wlroots compositors ship grim.
_BACKENDS = [
    ("spectacle", lambda path, region: ["spectacle", "-b", "-n",
                                        "-r" if region else "-f", "-o", path]),
    ("grim", lambda path, region: ["grim", path]),
    ("gnome-screenshot", lambda path, region: ["gnome-screenshot", "-f", path]),
    ("scrot", lambda path, region: ["scrot", "-o", path]),
    ("import", lambda path, region: ["import", "-window", "root", path]),
]


def capture(region: bool = False) -> Path:
    """Grab the screen to a PNG and return its path."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
    for binary, build in _BACKENDS:
        if not which(binary):
            continue
        # Region selection needs a human at the mouse, so allow longer.
        run(build(str(path), region), timeout=60 if region else 15)
        if path.exists() and path.stat().st_size > 0:
            _prune()
            return path
    raise CommandError(
        "no screenshot tool found — install spectacle (KDE) or grim (wlroots)")


def _prune(keep: int = 20) -> None:
    shots = sorted(SCREENSHOT_DIR.glob("screen-*.png"))
    for old in shots[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


@tool(description="Take a screenshot and save it. Use look_at_screen instead if "
                  "you need to answer a question about what is on screen.",
      risk="sensitive",
      params={"region": {"type": "boolean", "default": False,
                         "description": "Ask the user to drag a region."}})
def take_screenshot(ctx: ToolContext, region: bool = False) -> str:
    path = capture(region=region)
    return f"Screenshot saved to {path}."


@tool(description="Look at the user's screen and answer a question about it. Use "
                  "this for 'what is this', 'read this error', 'what am I looking "
                  "at' and anything else about the current screen contents.",
      risk="sensitive",
      params={"question": {"type": "string",
                           "description": "What to determine from the screen."}},
      required=["question"])
def look_at_screen(ctx: ToolContext, question: str) -> str:
    if ctx.brain is None:
        return "I have no vision model configured right now."
    from ..brain.base import BrainError, Message

    path = capture()
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    prompt = ("You are looking at a screenshot of the user's Linux desktop. "
              "Answer this in two sentences at most, as spoken text: " + question)
    try:
        reply = ctx.brain.reply(prompt, [Message.user_image(question, data)], [])
    except BrainError as exc:
        return (f"I could not read the screen: {exc} "
                "The configured model may not accept images.")
    return reply.text or "I could not make out anything useful on the screen."
