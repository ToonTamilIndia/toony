"""Window and session management. KWin on KDE, with generic fallbacks."""

from __future__ import annotations

import os

from .proc import CommandError, any_of, run, which
from .registry import ToolContext, tool


def _kdotool() -> str:
    binary = which("kdotool") or which("xdotool")
    if not binary:
        raise CommandError("window control needs kdotool (Wayland) or xdotool (X11)")
    return binary


@tool(description="List the titles of the currently open windows.",
      requires=("kdotool|xdotool",))
def list_windows(ctx: ToolContext) -> str:
    binary = _kdotool()
    if binary.endswith("kdotool"):
        ids = run(["kdotool", "search", ""]).split()
        titles = []
        for window_id in ids[:20]:
            try:
                titles.append(run(["kdotool", "getwindowname", window_id]))
            except CommandError:
                continue
    else:
        raw = run(["xdotool", "search", "--onlyvisible", "--name", ""])
        titles = []
        for window_id in raw.split()[:20]:
            try:
                titles.append(run(["xdotool", "getwindowname", window_id]))
            except CommandError:
                continue
    titles = [t for t in titles if t.strip()]
    if not titles:
        return "No windows are open."
    return f"{len(titles)} windows open: " + ", ".join(titles)


@tool(description="Bring a window to the front by part of its title.",
      risk="sensitive", params={"title": {"type": "string"}}, required=["title"],
      requires=("kdotool|xdotool",))
def focus_window(ctx: ToolContext, title: str) -> str:
    binary = _kdotool()
    name = os.path.basename(binary)
    matches = run([name, "search", title], check=False).split()
    if not matches:
        return f"I found no window matching {title}."
    run([name, "windowactivate", matches[0]])
    return f"Focused the {title} window."


@tool(description="Close the currently focused window.", risk="dangerous",
      requires=("kdotool|xdotool",))
def close_window(ctx: ToolContext) -> str:
    name = os.path.basename(_kdotool())
    active = run([name, "getactivewindow"])
    run([name, "windowclose", active])
    return "Window closed."


@tool(description="Show the current virtual desktop or switch to another one.",
      risk="sensitive",
      params={"number": {"type": "integer",
                         "description": "Desktop to switch to. Omit to just report."}},
      requires=("qdbus6|qdbus",))
def virtual_desktop(ctx: ToolContext, number: int | None = None) -> str:
    qdbus = any_of("qdbus6", "qdbus")
    service = ["org.kde.KWin", "/KWin"]
    if number is None:
        current = run([qdbus, *service, "currentDesktop"])
        return f"You are on virtual desktop {current}."
    run([qdbus, *service, "setCurrentDesktop", str(int(number))])
    return f"Switched to desktop {number}."
