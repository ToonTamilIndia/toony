"""Media playback control over MPRIS (playerctl)."""

from __future__ import annotations

from .proc import CommandError, run
from .registry import ToolContext, tool


@tool(description="Control media playback in whatever player is running "
                  "(Spotify, a browser, VLC, and so on).",
      params={"action": {"type": "string",
                         "enum": ["play", "pause", "play-pause", "next",
                                  "previous", "stop"]}},
      required=["action"], requires=("playerctl",))
def control_media(ctx: ToolContext, action: str) -> str:
    command = {"play": "play", "pause": "pause", "play-pause": "play-pause",
               "next": "next", "previous": "previous", "stop": "stop"}.get(action)
    if not command:
        return f"I do not know the playback action {action}."
    try:
        run(["playerctl", command])
    except CommandError as exc:
        if "No players found" in str(exc):
            return "Nothing is playing right now."
        raise
    return f"Playback: {action}."


@tool(description="Say what is currently playing.", requires=("playerctl",))
def now_playing(ctx: ToolContext) -> str:
    try:
        title = run(["playerctl", "metadata", "--format",
                     "{{title}} by {{artist}} ({{status}})"])
    except CommandError as exc:
        if "No players found" in str(exc):
            return "Nothing is playing right now."
        raise
    return title or "Nothing is playing right now."
