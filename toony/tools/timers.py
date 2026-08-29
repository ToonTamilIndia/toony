"""Timers, alarms and reminders.

These are handed to systemd rather than kept in a thread. A transient timer
unit outlives a daemon restart, survives an upgrade, and is visible in
``systemctl --user list-timers`` — none of which is true of a background thread,
and a reminder that quietly disappears is worse than no reminder at all.
"""

from __future__ import annotations

import re
import shutil
import sys
import time

from .proc import CommandError, run, which
from .registry import ToolContext, tool

_UNIT_PREFIX = "toony-timer"

_UNITS = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)"), 3600),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)"), 60),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)"), 1),
]
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
          "twelve": 12, "fifteen": 15, "twenty": 20, "twenty five": 25,
          "thirty": 30, "forty": 40, "forty five": 45, "forty-five": 45,
          "fifty": 50, "sixty": 60, "ninety": 90}

# Fractions of an hour never survive word-by-word substitution ("half an hour"
# would become "0.5 1 hour"), so they are rewritten as whole phrases first.
_PHRASES = [
    (re.compile(r"\bhalf\s+(?:an?\s+)?hour\b"), "30 minutes"),
    (re.compile(r"\b(?:a\s+)?quarter\s+(?:of\s+)?(?:an?\s+)?hour\b"), "15 minutes"),
    (re.compile(r"\bhalf\s+(?:a\s+)?minute\b"), "30 seconds"),
    (re.compile(r"\ban?\s+hour\s+and\s+a\s+half\b"), "90 minutes"),
]


def parse_duration(text: str) -> int:
    """Turn a spoken duration into seconds. Returns 0 if nothing was found.

    Speech recognition produces "five minutes" rather than "5m", so number
    words are substituted before the numeric patterns run.
    """
    lowered = " " + text.lower().strip() + " "
    for pattern, replacement in _PHRASES:
        lowered = pattern.sub(f" {replacement} ", lowered)
    for word, value in sorted(_WORDS.items(), key=lambda kv: -len(kv[0])):
        lowered = re.sub(rf"(?<![\w.]){re.escape(word)}(?=\s)",
                         f" {value:g}", lowered)

    total = 0.0
    for pattern, multiplier in _UNITS:
        for match in pattern.finditer(lowered):
            total += float(match.group(1)) * multiplier
    if total:
        return int(total)
    bare = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", lowered)
    return int(float(bare.group(1)) * 60) if bare else 0    # bare number = minutes


def _executable() -> list[str]:
    found = shutil.which("toony")
    return [found] if found else [sys.executable, "-m", "toony"]


def _speak_seconds(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if secs and not hours:
        parts.append(f"{secs} second" + ("s" if secs != 1 else ""))
    return " and ".join(parts) or "no time at all"


@tool(description="Set a timer or reminder. When it fires, Toony shows a "
                  "notification and says the message out loud. Use this for "
                  "'remind me in ten minutes', 'set a timer for five minutes', "
                  "'wake me in an hour'.",
      params={"duration": {"type": "string",
                           "description": "How long, e.g. '10 minutes', "
                                          "'1 hour 30 minutes', '90 seconds'."},
              "message": {"type": "string",
                          "description": "What to say when it fires."}},
      required=["duration"], requires=("systemd-run",))
def set_timer(ctx: ToolContext, duration: str, message: str = "") -> str:
    seconds = parse_duration(duration)
    if seconds <= 0:
        return (f"I could not work out how long '{duration}' is. "
                "Try something like 'ten minutes'.")
    if seconds > 86400:
        return "I only set timers up to a day long."
    text = message.strip() or f"Your {_speak_seconds(seconds)} timer is up."
    unit = f"{_UNIT_PREFIX}-{int(time.time())}"
    run(["systemd-run", "--user", "--quiet",
         f"--on-active={seconds}",
         f"--unit={unit}",
         "--timer-property=AccuracySec=1s",
         f"--description=Toony reminder: {text[:80]}",
         *_executable(), "remind", text], timeout=15)
    return f"Timer set for {_speak_seconds(seconds)}."


@tool(description="List the timers and reminders that are still pending.",
      requires=("systemctl",))
def list_timers(ctx: ToolContext) -> str:
    text = run(["systemctl", "--user", "list-timers", f"{_UNIT_PREFIX}-*",
                "--no-pager", "--no-legend"], timeout=15, check=False)
    rows = [line for line in text.splitlines() if _UNIT_PREFIX in line]
    if not rows:
        return "You have no timers running."
    out = []
    for row in rows[:10]:
        left = re.search(r"\s(\d+\w+(?:\s\d+\w+)?)\sleft", row)
        description = _description(row)
        out.append(f"{description} in {left.group(1)}" if left else description)
    return f"{len(rows)} pending: " + "; ".join(out) + "."


def _description(row: str) -> str:
    match = re.search(rf"({_UNIT_PREFIX}-\d+)\.timer", row)
    if not match:
        return "a timer"
    try:
        text = run(["systemctl", "--user", "show", f"{match.group(1)}.service",
                    "-p", "Description", "--value"], timeout=10)
    except CommandError:
        return "a timer"
    return text.replace("Toony reminder: ", "").strip() or "a timer"


@tool(description="Cancel a pending timer or all of them.", risk="sensitive",
      params={"which": {"type": "string",
                        "description": "Part of the reminder text, or 'all'."}},
      requires=("systemctl",))
def cancel_timer(ctx: ToolContext, which: str = "all") -> str:
    text = run(["systemctl", "--user", "list-timers", f"{_UNIT_PREFIX}-*",
                "--no-pager", "--no-legend"], timeout=15, check=False)
    units = re.findall(rf"({_UNIT_PREFIX}-\d+)\.timer", text)
    if not units:
        return "There are no timers to cancel."
    if which.strip().lower() not in ("all", ""):
        needle = which.lower()
        units = [u for u in units if needle in _description(f"{u}.timer").lower()]
        if not units:
            return f"No pending timer matches {which}."
    for unit in units:
        run(["systemctl", "--user", "stop", f"{unit}.timer"], timeout=10, check=False)
    return f"Cancelled {len(units)} timer" + ("s." if len(units) != 1 else ".")
