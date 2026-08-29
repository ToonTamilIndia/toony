"""Volume, power, time and machine facts."""

from __future__ import annotations

import datetime
import os
import platform
import shutil

from .proc import CommandError, any_of, run, which
from .registry import ToolContext, tool

_SINK = "@DEFAULT_AUDIO_SINK@"


def _volume_backend() -> str:
    backend = any_of("wpctl", "pactl")
    if not backend:
        raise CommandError("no PipeWire or PulseAudio control tool found")
    return os.path.basename(backend)


def _read_volume() -> tuple[int, bool]:
    if _volume_backend() == "wpctl":
        # "Volume: 0.35 [MUTED]"
        out = run(["wpctl", "get-volume", _SINK])
        parts = out.split()
        level = round(float(parts[1]) * 100)
        return level, "MUTED" in out
    out = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    level = int(out.split("/")[1].strip().rstrip("%"))
    muted = run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"]).endswith("yes")
    return level, muted


@tool(description="Read the current output volume as a percentage, and whether "
                  "the speakers are muted.",
      requires=("wpctl|pactl",))
def get_volume(ctx: ToolContext) -> str:
    level, muted = _read_volume()
    return f"Volume is {level} percent{' and muted' if muted else ''}."


@tool(description="Set the speaker output volume. Use this for 'louder', "
                  "'quieter', 'turn it up' and 'turn it down' as well — read the "
                  "current volume first if you need a relative change.",
      risk="safe",
      params={"level": {"type": "integer", "minimum": 0, "maximum": 100,
                        "description": "Target volume percentage, 0 to 100."}},
      required=["level"], requires=("wpctl|pactl",))
def set_volume(ctx: ToolContext, level: int) -> str:
    level = max(0, min(100, int(level)))
    if _volume_backend() == "wpctl":
        run(["wpctl", "set-volume", _SINK, f"{level}%"])
    else:
        run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
    return f"Volume set to {level} percent."


@tool(description="Mute, unmute, or toggle the speakers.",
      params={"state": {"type": "string", "enum": ["mute", "unmute", "toggle"]}},
      required=["state"], requires=("wpctl|pactl",))
def set_mute(ctx: ToolContext, state: str) -> str:
    value = {"mute": "1", "unmute": "0", "toggle": "toggle"}.get(state, "toggle")
    if _volume_backend() == "wpctl":
        run(["wpctl", "set-mute", _SINK, value])
    else:
        run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", value])
    return f"Speakers {state}d." if state != "toggle" else "Mute toggled."


@tool(description="Set the laptop screen brightness as a percentage.",
      params={"level": {"type": "integer", "minimum": 1, "maximum": 100}},
      required=["level"], requires=("brightnessctl",))
def set_brightness(ctx: ToolContext, level: int) -> str:
    level = max(1, min(100, int(level)))
    run(["brightnessctl", "set", f"{level}%"])
    return f"Brightness set to {level} percent."


@tool(description="Get the current date and time.")
def get_datetime(ctx: ToolContext) -> str:
    now = datetime.datetime.now().astimezone()
    return now.strftime("It is %H:%M on %A, %d %B %Y (%Z).")


@tool(description="Report battery charge and whether the laptop is plugged in.")
def get_battery(ctx: ToolContext) -> str:
    base = "/sys/class/power_supply"
    if not os.path.isdir(base):
        return "This machine has no battery information."
    for entry in sorted(os.listdir(base)):
        path = os.path.join(base, entry)
        try:
            with open(os.path.join(path, "type")) as fh:
                if fh.read().strip() != "Battery":
                    continue
            with open(os.path.join(path, "capacity")) as fh:
                capacity = fh.read().strip()
            with open(os.path.join(path, "status")) as fh:
                status = fh.read().strip().lower()
        except OSError:
            continue
        return f"Battery is at {capacity} percent and {status}."
    return "No battery found — this looks like a desktop or the battery is not reporting."


@tool(description="Report CPU load, memory use and free disk space.")
def get_system_info(ctx: ToolContext) -> str:
    load = ", ".join(f"{v:.2f}" for v in os.getloadavg())
    memory = _meminfo()
    usage = shutil.disk_usage(os.path.expanduser("~"))
    free_gb = usage.free / 1e9
    return (f"{platform.node()} running {platform.system()} {platform.release()}. "
            f"Load average {load}. {memory} "
            f"{free_gb:.1f} gigabytes free in your home directory.")


def _meminfo() -> str:
    try:
        values = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                values[key] = int(rest.strip().split()[0]) * 1024
        total, available = values["MemTotal"], values["MemAvailable"]
        used = total - available
        return f"Memory {used / 1e9:.1f} of {total / 1e9:.1f} gigabytes in use."
    except (OSError, KeyError, ValueError):
        return ""


@tool(description="Show a desktop notification. Use only when the user asks to be "
                  "reminded of something on screen; normally just speak the answer.",
      params={"title": {"type": "string"}, "body": {"type": "string"}},
      required=["title"], requires=("notify-send",))
def notify(ctx: ToolContext, title: str, body: str = "") -> str:
    run(["notify-send", "-a", "Toony", title, body])
    return "Notification shown."


@tool(description="Lock the screen.", risk="sensitive")
def lock_screen(ctx: ToolContext) -> str:
    if which("loginctl"):
        run(["loginctl", "lock-session"])
    elif which("qdbus6") or which("qdbus"):
        run([any_of("qdbus6", "qdbus"), "org.freedesktop.ScreenSaver",
             "/ScreenSaver", "Lock"])
    else:
        raise CommandError("no screen locker available")
    return "Screen locked."
