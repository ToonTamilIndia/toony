"""Suspending, locking and shutting down.

These are the tools where a misheard word costs the most — "shut down" and
"shut up" differ by one syllable — so everything that loses work is
``dangerous`` and, with the default policy, refused outright until the user
deliberately moves it into ``tools.always_ask``.
"""

from __future__ import annotations

from .proc import CommandError, any_of, run, spawn, which
from .registry import ToolContext, tool


def _loginctl(action: str) -> None:
    if which("systemctl"):
        run(["systemctl", action], timeout=15, check=False)
        return
    raise CommandError("systemctl is not available")


@tool(description="Put the computer to sleep.", risk="dangerous",
      requires=("systemctl",))
def suspend(ctx: ToolContext) -> str:
    _loginctl("suspend")
    return "Going to sleep."


@tool(description="Hibernate the computer to disk.", risk="dangerous",
      requires=("systemctl",))
def hibernate(ctx: ToolContext) -> str:
    _loginctl("hibernate")
    return "Hibernating."


@tool(description="Restart the computer. Only when the user clearly asks to "
                  "reboot or restart the machine.",
      risk="dangerous", requires=("systemctl",))
def reboot(ctx: ToolContext) -> str:
    _loginctl("reboot")
    return "Restarting now."


@tool(description="Shut the computer down completely.", risk="dangerous",
      requires=("systemctl",))
def power_off(ctx: ToolContext) -> str:
    _loginctl("poweroff")
    return "Shutting down."


@tool(description="Log out of the desktop session.", risk="dangerous")
def log_out(ctx: ToolContext) -> str:
    for argv in (["qdbus6", "org.kde.Shutdown", "/Shutdown", "logout"],
                 ["qdbus", "org.kde.Shutdown", "/Shutdown", "logout"],
                 ["loginctl", "terminate-session", "self"]):
        if which(argv[0]):
            run(argv, timeout=10, check=False)
            return "Logging out."
    raise CommandError("no way to log out on this desktop")


@tool(description="Set the power profile: power-saver, balanced or performance.",
      risk="sensitive",
      params={"profile": {"type": "string",
                          "enum": ["power-saver", "balanced", "performance"]}},
      required=["profile"], requires=("powerprofilesctl",))
def set_power_profile(ctx: ToolContext, profile: str) -> str:
    run(["powerprofilesctl", "set", profile], timeout=10)
    return f"Switched to the {profile.replace('-', ' ')} power profile."


@tool(description="Report the current power profile.",
      requires=("powerprofilesctl",))
def get_power_profile(ctx: ToolContext) -> str:
    current = run(["powerprofilesctl", "get"], timeout=10)
    return f"The power profile is {current.replace('-', ' ')}."


@tool(description="Turn night colour, the warm evening screen tint, on or off.",
      risk="sensitive",
      params={"state": {"type": "string", "enum": ["on", "off", "toggle"]}},
      required=["state"])
def night_colour(ctx: ToolContext, state: str) -> str:
    qdbus = any_of("qdbus6", "qdbus-qt6", "qdbus")
    if not qdbus:
        raise CommandError("qdbus is not installed")
    run([qdbus, "org.kde.KWin.NightLight", "/ColorCorrect", "toggle"],
        timeout=10, check=False)
    return "Toggled night colour."


@tool(description="Start the screen saver or turn the screen off to save power.",
      risk="sensitive")
def screen_off(ctx: ToolContext) -> str:
    for argv in (["kscreen-doctor", "--dpms", "off"],
                 ["xset", "dpms", "force", "off"]):
        if which(argv[0]):
            spawn(argv)
            return "Turning the screen off."
    raise CommandError("no way to blank the screen on this session")
