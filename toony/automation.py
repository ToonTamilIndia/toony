"""Routines: things Toony does without being asked.

A voice assistant that only answers questions is a search engine with a
microphone. The useful half is the half that runs on its own — check for
updates before you sit down, tell you the battery is about to die, say what
broke overnight.

A routine is a trigger and a prompt. The prompt goes through the ordinary
agent, which means it gets the ordinary tools *and the ordinary permission
layer*: a routine cannot do anything you could not have asked for out loud, and
anything that would have asked permission still asks. That is the whole safety
story, and it is deliberately not configurable.

Triggers:

``at 08:30``          every day at that time, optionally only on some days
``every 30m``         repeating, from when the daemon started
``on startup``        once, shortly after the daemon comes up
``on network_up``     when the machine gets a connection back
``on network_down``   when it loses one
``on battery_low``    when the battery first drops below the threshold

Everything is stored in the config file, so ``toony routine add`` and the GUI
edit the same list.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .log import get

log = get("automation")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
EVENTS = ("startup", "network_up", "network_down", "battery_low")

_INTERVAL = re.compile(r"^every\s+(\d+(?:\.\d+)?)\s*([smhd])\w*$", re.I)
_AT = re.compile(r"^at\s+(\d{1,2}):(\d{2})$", re.I)
_ON = re.compile(r"^on\s+([a-z_]+)$", re.I)

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class BadRoutine(ValueError):
    """The trigger or the routine does not make sense — said in words."""


@dataclass
class Trigger:
    kind: str                       # interval | daily | event
    seconds: float = 0.0
    hour: int = 0
    minute: int = 0
    event: str = ""

    def describe(self) -> str:
        if self.kind == "interval":
            return f"every {_pretty(self.seconds)}"
        if self.kind == "daily":
            return f"at {self.hour:02d}:{self.minute:02d}"
        return f"on {self.event}"


def parse_trigger(text: str) -> Trigger:
    text = str(text or "").strip()
    if not text:
        raise BadRoutine("a routine needs a trigger, like 'every 30m' or "
                         "'at 08:30'")

    match = _INTERVAL.match(text)
    if match:
        seconds = float(match.group(1)) * UNITS[match.group(2).lower()]
        if seconds < 60:
            raise BadRoutine("the shortest interval is one minute — anything "
                             "faster is a loop, not a routine")
        return Trigger("interval", seconds=seconds)

    match = _AT.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise BadRoutine(f"{text!r} is not a time of day")
        return Trigger("daily", hour=hour, minute=minute)

    match = _ON.match(text)
    if match:
        event = match.group(1).lower()
        if event not in EVENTS:
            raise BadRoutine(f"{event!r} is not something I can watch for. "
                             f"Try one of: {', '.join(EVENTS)}")
        return Trigger("event", event=event)

    raise BadRoutine(
        f"I could not read {text!r} as a trigger. Use 'every 30m', "
        f"'at 08:30', or 'on startup'.")


@dataclass
class Routine:
    name: str
    trigger: Trigger
    prompt: str
    speak: bool = True
    enabled: bool = True
    days: tuple[str, ...] = ()
    # State, not configuration.
    next_run: float = 0.0
    last_run: float = 0.0
    runs: int = 0
    last_result: str = ""
    last_error: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "Routine":
        name = str(raw.get("name", "")).strip()
        prompt = str(raw.get("prompt", "")).strip()
        if not name:
            raise BadRoutine("a routine needs a name")
        if not prompt:
            raise BadRoutine(f"{name!r} has nothing to do — it needs a prompt")
        days = tuple(str(d).strip().lower()[:3] for d in (raw.get("days") or []))
        for day in days:
            if day not in DAYS:
                raise BadRoutine(f"{day!r} is not a day. Use: {', '.join(DAYS)}")
        return cls(name=name, trigger=parse_trigger(raw.get("when", "")),
                   prompt=prompt, speak=bool(raw.get("speak", True)),
                   enabled=bool(raw.get("enabled", True)), days=days)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"name": self.name,
                               "when": self.trigger.describe(),
                               "prompt": self.prompt, "speak": self.speak,
                               "enabled": self.enabled}
        if self.days:
            out["days"] = list(self.days)
        return out

    def runs_today(self, now: datetime) -> bool:
        return not self.days or DAYS[now.weekday()] in self.days

    def describe(self) -> str:
        state = "" if self.enabled else " (off)"
        when = self.trigger.describe()
        if self.days:
            when += " on " + ",".join(self.days)
        return f"{self.name}{state}: {when} -> {self.prompt}"


def load(config) -> list[Routine]:
    """Every routine in the config file. A broken one is skipped, not fatal."""
    out: list[Routine] = []
    for raw in (config.get("automation.routines", []) or []):
        if not isinstance(raw, dict):
            continue
        try:
            out.append(Routine.from_dict(raw))
        except BadRoutine as exc:
            log.error("ignoring routine %r: %s", raw.get("name", "?"), exc)
    return out


def save(config, routines: list[Routine], persist: bool = True) -> None:
    config.set("automation.routines", [r.to_dict() for r in routines],
               save=persist)


class Scheduler:
    """Runs routines when they are due. One thread, one at a time."""

    def __init__(self, config, run: Callable[[Routine], str],
                 publish: Callable[..., None] | None = None):
        self.config = config
        self.run = run
        self.publish = publish or (lambda *a, **k: None)
        self.routines: list[Routine] = []
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._wake = threading.Event()
        self._fired_events: set[str] = set()

    # ---- settings ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("automation.enabled", True))

    @property
    def tick(self) -> float:
        return max(5.0, float(self.config.get("automation.tick_s", 30)))

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        """Whether Toony should keep its voice down right now.

        Quiet hours do not stop a routine running — a battery warning that
        does not fire is worse than one that arrives on screen. They stop it
        speaking.
        """
        window = str(self.config.get("automation.quiet_hours", "") or "").strip()
        if not window or "-" not in window:
            return False
        start_text, _, end_text = window.partition("-")
        try:
            start = _time_of_day(start_text)
            end = _time_of_day(end_text)
        except ValueError:
            log.warning("automation.quiet_hours %r is not a time range like "
                        "22:00-07:30", window)
            return False
        minutes = _minutes(now or datetime.now())
        if start <= end:
            return start <= minutes < end
        return minutes >= start or minutes < end      # crosses midnight

    # ---- lifecycle --------------------------------------------------------
    def reload(self) -> None:
        previous = {r.name: r for r in self.routines}
        self.routines = load(self.config)
        now = time.monotonic()
        for routine in self.routines:
            old = previous.get(routine.name)
            if old is not None and old.trigger == routine.trigger:
                routine.next_run = old.next_run
                routine.runs, routine.last_run = old.runs, old.last_run
            else:
                routine.next_run = self._first_run(routine, now)
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.reload()
        if not self.routines:
            return
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="toony-routines",
                                        daemon=True)
        self._thread.start()
        log.info("%d routine(s) scheduled", len(self.routines))

    def stop(self) -> None:
        self._running.clear()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    # ---- events -----------------------------------------------------------
    def fire(self, event: str) -> int:
        """Something happened. Run whatever was waiting for it."""
        if not self.enabled:
            return 0
        due = [r for r in self.routines
               if r.enabled and r.trigger.kind == "event"
               and r.trigger.event == event]
        for routine in due:
            self._execute(routine)
        return len(due)

    # ---- the loop ---------------------------------------------------------
    def _first_run(self, routine: Routine, now: float) -> float:
        if routine.trigger.kind == "interval":
            return now + routine.trigger.seconds
        if routine.trigger.kind == "daily":
            return now + _seconds_until(routine.trigger, datetime.now())
        return 0.0      # events are not scheduled

    def _loop(self) -> None:
        while self._running.is_set():
            self._wake.wait(self.tick)
            self._wake.clear()
            if not self._running.is_set():
                break
            if not self.enabled:
                continue
            now = time.monotonic()
            for routine in list(self.routines):
                if not routine.enabled or routine.trigger.kind == "event":
                    continue
                if routine.next_run and now < routine.next_run:
                    continue
                if not routine.runs_today(datetime.now()):
                    routine.next_run = now + _seconds_until(routine.trigger,
                                                            datetime.now())
                    continue
                self._execute(routine)
                routine.next_run = now + (
                    routine.trigger.seconds if routine.trigger.kind == "interval"
                    else _seconds_until(routine.trigger, datetime.now()))

    def _execute(self, routine: Routine) -> str:
        log.info("routine %r: %s", routine.name, routine.prompt[:80])
        routine.last_run = time.monotonic()
        routine.runs += 1
        self.publish("routine", name=routine.name, prompt=routine.prompt)
        try:
            result = self.run(routine) or ""
            routine.last_result = result
            routine.last_error = ""
        except Exception as exc:
            routine.last_error = str(exc)
            log.exception("routine %r failed", routine.name)
            return ""
        self.publish("routine_done", name=routine.name, result=result[:400])
        return result

    def status(self) -> dict:
        now = time.monotonic()
        return {"enabled": self.enabled, "quiet": self.in_quiet_hours(),
                "routines": [
                    {"name": r.name, "when": r.trigger.describe(),
                     "enabled": r.enabled, "runs": r.runs,
                     "in_s": round(max(0.0, r.next_run - now), 1)
                             if r.next_run else None,
                     "error": r.last_error}
                    for r in self.routines]}


class Watcher:
    """Turns changes in the machine's condition into routine triggers.

    Only what can be read without asking anyone's permission: whether there is
    a network, and what the battery is doing. Both come from files, so this
    costs nothing to run on a timer.

    Each event fires on the *edge*. A battery at 12% does not fire
    ``battery_low`` every thirty seconds all afternoon; it fires once when it
    crosses the line, and re-arms when it goes back above it.
    """

    def __init__(self, config, fire: Callable[[str], Any]):
        self.config = config
        self.fire = fire
        self._online: bool | None = None
        self._low = False
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._wake = threading.Event()

    @property
    def threshold(self) -> int:
        return int(self.config.get("automation.battery_low_percent", 20))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="toony-watch",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _loop(self) -> None:
        # The first pass only records where things stand. Firing
        # "network_down" at startup because the probe has not run yet would be
        # a lie every single boot.
        self.poll(announce=False)
        while self._running.is_set():
            self._wake.wait(max(10.0, float(
                self.config.get("automation.watch_s", 45))))
            self._wake.clear()
            if self._running.is_set():
                self.poll()

    def poll(self, announce: bool = True) -> list[str]:
        """Read the world once. Returns the events that fired."""
        fired: list[str] = []
        from .net import NETWORK

        try:
            now_online = NETWORK.online()
        except Exception:
            now_online = self._online
        if now_online is not None and now_online != self._online:
            if self._online is not None and announce:
                fired.append("network_up" if now_online else "network_down")
            self._online = now_online

        level = battery_percent()
        if level is not None:
            if level <= self.threshold and not self._low:
                self._low = True
                if announce and not on_mains():
                    fired.append("battery_low")
            elif level > self.threshold + 5:
                self._low = False       # hysteresis, so it cannot flap

        for event in fired:
            try:
                self.fire(event)
            except Exception:
                log.exception("could not run routines for %s", event)
        return fired


def battery_percent() -> int | None:
    """The battery level, or None on a machine that has no battery."""
    import glob

    for path in sorted(glob.glob("/sys/class/power_supply/BAT*/capacity")):
        try:
            with open(path) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            continue
    return None


def on_mains() -> bool:
    """Whether it is plugged in. A low battery on the charger is not news."""
    import glob

    for path in sorted(glob.glob("/sys/class/power_supply/A*/online")):
        try:
            with open(path) as fh:
                if fh.read().strip() == "1":
                    return True
        except OSError:
            continue
    return False


# ---- little helpers -------------------------------------------------------
def _minutes(when: datetime) -> int:
    return when.hour * 60 + when.minute


def _time_of_day(text: str) -> int:
    hour, _, minute = text.strip().partition(":")
    value = int(hour) * 60 + int(minute or 0)
    if not 0 <= value < 24 * 60:
        raise ValueError(text)
    return value


def _seconds_until(trigger: Trigger, now: datetime) -> float:
    target = now.replace(hour=trigger.hour, minute=trigger.minute, second=0,
                         microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _pretty(seconds: float) -> str:
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"
