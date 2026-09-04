"""Watching the push-to-talk key directly, for press *and* release.

KDE's global shortcuts are the polite way to get a hotkey, and they have two
problems that matter here.

The first is that a command shortcut is fire-and-forget: kglobalaccel runs
``toony listen`` when the combination goes down and tells nobody when it comes
back up. There is no release event to be had, so "hold the key while you talk"
— the mode most people actually want — cannot be built on it at all.

The second is latency. The press goes to the compositor, which spawns a
process, which loads Python, which connects to a socket. That is sixty to a
hundred and fifty milliseconds before the daemon knows anything, on top of
however long the microphone takes to open.

Reading ``/dev/input/event*`` costs about ten milliseconds and gives both
edges. The catch is permission: the device nodes belong to the ``input`` group.
:func:`diagnose` explains exactly what is wrong and how to fix it, and the
whole thing degrades to the KDE shortcut when it cannot be used.

No dependency: an input event is a 24-byte struct and the capability bitmaps
are readable text under /sys.
"""

from __future__ import annotations

import glob
import os
import select
import struct
import threading
import time
from typing import Callable

from ..log import get

log = get("audio.hotkey")

# struct input_event on 64-bit Linux: two longs, two shorts, one int.
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 0x01

RELEASE, PRESS, REPEAT = 0, 1, 2

# From linux/input-event-codes.h. Only the keys somebody would bind.
KEYS: dict[str, int] = {
    "esc": 1, "escape": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10,
    "0": 11, "minus": 12, "equal": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23,
    "o": 24, "p": 25, "enter": 28, "return": 28,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37,
    "l": 38, "semicolon": 39, "apostrophe": 40, "grave": 41, "backslash": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    "comma": 51, "period": 52, "slash": 53,
    "space": 57, "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65,
    "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "insert": 110, "delete": 111, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "pause": 119, "menu": 127, "scrolllock": 70, "printscreen": 99,
}

# Each modifier is a pair — either side of the keyboard will do.
MODIFIERS: dict[str, tuple[int, ...]] = {
    "ctrl": (29, 97), "control": (29, 97),
    "shift": (42, 54),
    "alt": (56, 100),
    "meta": (125, 126), "super": (125, 126), "win": (125, 126),
}

# A modifier on its own is a legitimate push-to-talk key — "hold right control"
# is the convention every game voice chat uses.
MODIFIER_AS_KEY = {
    "rightctrl": 97, "leftctrl": 29, "rightalt": 100, "leftalt": 56,
    "rightshift": 54, "leftshift": 42, "rightmeta": 126, "leftmeta": 125,
}


class HotkeyUnavailable(RuntimeError):
    pass


def parse(shortcut: str) -> tuple[frozenset[int], tuple[int, ...]]:
    """"Meta+Space" -> (the modifier codes required, the codes that trigger).

    Modifiers come back as one flat set: any of the listed codes satisfies its
    modifier, which is what "either Ctrl" means.
    """
    parts = [p.strip().lower() for p in str(shortcut).split("+") if p.strip()]
    if not parts:
        raise HotkeyUnavailable("no shortcut is set")
    *mods, key = parts

    required: set[int] = set()
    for mod in mods:
        codes = MODIFIERS.get(mod)
        if codes is None:
            raise HotkeyUnavailable(f"{mod!r} is not a modifier I know")
        required.update(codes)

    if key in MODIFIER_AS_KEY:
        return frozenset(required), (MODIFIER_AS_KEY[key],)
    if key in MODIFIERS and not mods:
        return frozenset(), MODIFIERS[key]
    code = KEYS.get(key)
    if code is None:
        raise HotkeyUnavailable(
            f"{key!r} is not a key I know how to watch for. Try a letter, a "
            f"function key, space, or a named modifier like rightctrl.")
    return frozenset(required), (code,)


# ---- finding keyboards ----------------------------------------------------
def _capability_bits(path: str) -> set[int]:
    """The key codes a device can produce, from its /sys capability bitmap."""
    try:
        with open(path, "r") as fh:
            words = fh.read().strip().split()
    except OSError:
        return set()
    bits: set[int] = set()
    # The bitmap is printed most-significant word first, 64 bits per word.
    for index, word in enumerate(reversed(words)):
        try:
            value = int(word, 16)
        except ValueError:
            continue
        base = index * 64
        while value:
            low = value & -value
            bits.add(base + low.bit_length() - 1)
            value ^= low
    return bits


def _device_name(event: str) -> str:
    try:
        with open(f"/sys/class/input/{event}/device/name") as fh:
            return fh.read().strip()
    except OSError:
        return event


def keyboards() -> list[tuple[str, str]]:
    """Every input device that looks like a keyboard, as (path, name)."""
    found: list[tuple[str, str]] = []
    for node in sorted(glob.glob("/dev/input/event*")):
        event = os.path.basename(node)
        keys = _capability_bits(f"/sys/class/input/{event}/device/capabilities/key")
        # A real keyboard has letters and a space bar. This rejects power
        # buttons, lid switches and the "consumer control" endpoint every USB
        # receiver presents alongside the actual keyboard.
        if not keys or not {KEYS["a"], KEYS["z"], KEYS["space"]} <= keys:
            continue
        found.append((node, _device_name(event)))
    return found


def readable() -> list[tuple[str, str]]:
    return [(path, name) for path, name in keyboards() if os.access(path, os.R_OK)]


def usable() -> bool:
    return bool(readable())


def diagnose() -> str:
    """Why this is or is not going to work, in words that suggest a fix."""
    all_boards = keyboards()
    if not all_boards:
        return ("no keyboard devices found under /dev/input — this is normal "
                "inside a container, and the KDE shortcut will be used instead")
    ok = readable()
    if ok:
        names = ", ".join(name for _, name in ok[:3])
        return f"reading {len(ok)} keyboard(s) directly: {names}"
    return (f"found {len(all_boards)} keyboard(s) but cannot read them. "
            f"The device nodes belong to the 'input' group:\n"
            f"  sudo usermod -aG input $USER\n"
            f"then log out and back in. Until then the KDE shortcut is used, "
            f"and push-to-talk 'hold' mode will not work.")


# ---- the listener ---------------------------------------------------------
class HotkeyListener:
    """Calls back on press and release of one key combination.

    The devices are *read*, never grabbed: grabbing would take the key away
    from everything else on the desktop, which is not a trade anybody wants for
    a talk button.
    """

    def __init__(self, shortcut: str, on_press: Callable[[], None],
                 on_release: Callable[[], None] | None = None,
                 devices: list[str] | None = None):
        self.shortcut = shortcut
        self.modifiers, self.triggers = parse(shortcut)
        self.on_press = on_press
        self.on_release = on_release
        self._explicit = devices
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._wake_r = self._wake_w = -1
        self._held: set[int] = set()
        self._down = False
        self.presses = 0
        self.last_press = 0.0
        self.devices: list[str] = []

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.active:
            return
        paths = self._explicit or [p for p, _ in readable()]
        if not paths:
            raise HotkeyUnavailable(diagnose())
        self.devices = paths
        self._wake_r, self._wake_w = os.pipe()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="toony-hotkey",
                                        daemon=True)
        self._thread.start()
        log.info("watching %s on %d keyboard(s)", self.shortcut, len(paths))

    def stop(self) -> None:
        self._running.clear()
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1

    # ---- the read loop ----------------------------------------------------
    def _loop(self) -> None:
        handles: dict[int, object] = {}
        for path in self.devices:
            try:
                handles[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
            except OSError as exc:
                log.debug("cannot open %s: %s", path, exc)
        if not handles:
            log.warning("no keyboard could be opened — push-to-talk falls back "
                        "to the desktop shortcut")
            return

        watched = list(handles) + [self._wake_r]
        try:
            while self._running.is_set():
                try:
                    ready, _, _ = select.select(watched, [], [], 1.0)
                except (OSError, ValueError):
                    break
                for fd in ready:
                    if fd == self._wake_r:
                        return
                    self._drain(fd, handles)
        finally:
            for fd in handles:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _drain(self, fd: int, handles: dict) -> None:
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return
        except OSError as exc:
            log.debug("%s went away: %s", handles.get(fd), exc)
            return
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _, _, kind, code, value = struct.unpack_from(EVENT_FORMAT, data,
                                                         offset)
            if kind == EV_KEY:
                self._key(code, value)

    def _key(self, code: int, value: int) -> None:
        if value == PRESS:
            self._held.add(code)
        elif value == RELEASE:
            self._held.discard(code)
        elif value == REPEAT:
            return          # holding a key down is not pressing it again

        if code not in self.triggers:
            return
        if value == PRESS:
            if self.modifiers and not (self._held & self.modifiers):
                return
            if self._down:
                return
            self._down = True
            self.presses += 1
            self.last_press = time.monotonic()
            self._fire(self.on_press, "press")
        elif value == RELEASE and self._down:
            self._down = False
            self._fire(self.on_release, "release")

    @staticmethod
    def _fire(callback: Callable[[], None] | None, what: str) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            log.exception("push-to-talk %s handler raised", what)


def build(config, on_press: Callable[[], None],
          on_release: Callable[[], None] | None = None):
    """A listener, or None when this engine is not wanted or not possible."""
    if not config.get("ptt.enabled", True):
        return None
    engine = str(config.get("ptt.engine", "auto")).lower()
    if engine == "shortcut":
        return None
    if engine == "auto" and not usable():
        log.info("push-to-talk: %s", diagnose())
        return None

    device = str(config.get("ptt.device", "")).strip()
    devices = [device] if device else None
    try:
        return HotkeyListener(str(config.get("ptt.shortcut", "Meta+Space")),
                              on_press, on_release, devices=devices)
    except HotkeyUnavailable as exc:
        if engine == "evdev":
            raise
        log.info("push-to-talk stays on the desktop shortcut: %s", exc)
        return None
