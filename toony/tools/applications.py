"""Finding and launching desktop applications."""

from __future__ import annotations

import configparser
import os
import re
import shlex
import time
from pathlib import Path

from ..log import get
from .proc import CommandError, any_of, launch, which
from .registry import ToolContext, tool

log = get("tools.applications")

_XDG_DIRS = [
    Path("~/.local/share/applications").expanduser(),
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("~/.local/share/flatpak/exports/share/applications").expanduser(),
    Path("/var/lib/snapd/desktop/applications"),
]


#: (fingerprint, read at, apps). None until the first read.
_CACHE: tuple[tuple, float, list[dict[str, str]]] | None = None

#: Re-read this often even when nothing looks changed, so an entry rewritten
#: in place — a Flatpak update, say — is picked up too.
_MAX_AGE_S = 60.0


def _stamp() -> tuple:
    """A cheap fingerprint of the application directories.

    The index used to be cached for the life of the daemon, so an application
    installed this afternoon stayed invisible until Toony was restarted.

    Both halves earn their place: a directory's modification time changes when
    anything is installed or removed, and the number of entries catches the
    case where two changes land inside one tick of the filesystem's clock —
    which is most of them, on a filesystem with one-second timestamps.
    """
    marks = []
    for directory in _XDG_DIRS:
        try:
            marks.append((directory.stat().st_mtime_ns,
                          sum(1 for _ in directory.glob("*.desktop"))))
        except OSError:
            marks.append((0, 0))
    return tuple(marks)


def _index() -> list[dict[str, str]]:
    """Every launchable .desktop file, re-read whenever one is added."""
    global _CACHE
    stamp = _stamp()
    if (_CACHE is not None and _CACHE[0] == stamp
            and time.monotonic() - _CACHE[1] < _MAX_AGE_S):
        return _CACHE[2]
    apps: dict[str, dict[str, str]] = {}
    for directory in _XDG_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.desktop")):
            entry = _parse(path)
            if entry and entry["id"] not in apps:
                apps[entry["id"]] = entry
    _CACHE = (stamp, time.monotonic(), list(apps.values()))
    return _CACHE[2]


def _parse(path: Path) -> dict[str, str] | None:
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
        section = parser["Desktop Entry"]
    except Exception:
        return None
    if section.get("Type") != "Application":
        return None
    if section.get("NoDisplay", "false").lower() == "true":
        return None
    if section.get("Hidden", "false").lower() == "true":
        return None
    name = section.get("Name", "").strip()
    if not name:
        return None
    # TryExec names the binary that has to exist for the entry to be usable.
    # Honouring it keeps leftover .desktop files from uninstalled packages out
    # of the index, which is where half the "I opened it" lies came from.
    try_exec = section.get("TryExec", "").strip()
    if try_exec and not (which(try_exec) or Path(try_exec).exists()):
        return None
    return {
        "id": path.stem,
        "name": name,
        "comment": section.get("Comment", "").strip(),
        "exec": _command(section.get("Exec", "")),
        "keywords": section.get("Keywords", ""),
        "terminal": section.get("Terminal", "false").strip().lower() == "true",
        "path": str(path),
    }


def _command(line: str) -> str:
    """An Exec= line with the field codes removed.

    %% is a literal percent sign, so it has to come out of the way before the
    codes are stripped or "50%%" turns into "50" plus whatever follows.
    """
    line = line.replace("%%", "\x00")
    line = re.sub(r"%[fFuUdDnNickvm]", "", line)
    return line.replace("\x00", "%").strip()


def _score(entry: dict[str, str], query: str) -> int:
    """Rank a .desktop entry against a spoken application name."""
    query = query.lower().strip()
    name = entry["name"].lower()
    if name == query or entry["id"].lower() == query:
        return 100
    if name.startswith(query) or entry["id"].lower().startswith(query):
        return 80
    if query in name:
        return 60
    if query in entry["id"].lower():
        return 50
    haystack = f"{entry['comment']} {entry['keywords']}".lower()
    if query in haystack:
        return 30
    # Spoken names lose spaces and case: "libre office" -> "libreoffice".
    if query.replace(" ", "") in name.replace(" ", ""):
        return 40
    return 0


def find_app(query: str) -> dict[str, str] | None:
    ranked = sorted(((_score(e, query), e) for e in _index()),
                    key=lambda pair: pair[0], reverse=True)
    if ranked and ranked[0][0] > 0:
        return ranked[0][1]
    return None


@tool(description="Launch a desktop application by name, for example 'firefox', "
                  "'code', 'spotify' or 'system settings'.",
      risk="sensitive",
      params={"name": {"type": "string",
                       "description": "Application name as the user said it."}},
      required=["name"])
def open_application(ctx: ToolContext, name: str) -> str:
    entry = find_app(name)
    if not entry:
        return (f"I could not find an application called {name}. "
                "Use list_applications to see what is installed.")
    _launch_entry(entry)
    return f"Opened {entry['name']}."


def _launch_entry(entry: dict) -> None:
    """Start an application, trying each launcher until one actually works.

    This used to pick a single launcher and assume it succeeded. If the chosen
    one refused — no gio on the session bus, a .desktop id gtk-launch could not
    resolve — nothing opened and Toony still said it had. Now every candidate
    is tried in turn and only a run of failures is reported, with the reason
    the last one gave.
    """
    reasons = []
    for argv in _candidates(entry):
        try:
            launch(argv, entry["name"])
            return
        except CommandError as exc:
            log.info("%s could not open %s: %s", argv[0], entry["name"], exc)
            reasons.append(f"{os.path.basename(argv[0])}: {exc}")
    if not reasons:
        raise CommandError(f"there is no way to launch {entry['name']} on "
                           "this machine — its desktop entry has no Exec line")
    raise CommandError(f"{entry['name']} would not start. {reasons[-1]}")


def _candidates(entry: dict) -> list[list[str]]:
    """Every way we know of to start this entry, best first.

    The .desktop-aware launchers come first because they honour the whole
    entry — the working directory, Terminal=true, D-Bus activation — and the
    raw Exec line is the last resort rather than the first guess.
    """
    argv: list[list[str]] = []
    if which("gio"):
        argv.append(["gio", "launch", entry["path"]])
    kioclient = any_of("kioclient6", "kioclient")
    if kioclient:
        argv.append([kioclient, "exec", entry["path"]])
    if which("gtk-launch"):
        argv.append(["gtk-launch", entry["id"]])
    if entry.get("exec"):
        try:
            command = shlex.split(entry["exec"])
        except ValueError:
            # An unbalanced quote in someone else's .desktop file is not worth
            # failing over; the launchers above still had their turn.
            command = entry["exec"].split()
        if command:
            argv.append(_in_terminal(command) if entry.get("terminal")
                        else command)
    return argv


def _in_terminal(command: list[str]) -> list[str]:
    """Wrap a Terminal=true entry, which cannot just be exec'd on its own."""
    terminal = any_of("konsole", "x-terminal-emulator", "gnome-terminal",
                      "xterm")
    return [terminal, "-e", *command] if terminal else command


@tool(description="List installed applications, optionally filtered by a search "
                  "word. Use this when the user asks what is installed.",
      params={"query": {"type": "string", "description": "Optional filter."},
              "limit": {"type": "integer", "default": 15}})
def list_applications(ctx: ToolContext, query: str = "", limit: int = 15) -> str:
    apps = _index()
    if query:
        apps = [e for e in apps if _score(e, query) > 0]
    names = sorted({e["name"] for e in apps})[:max(1, min(50, limit))]
    if not names:
        return "No matching applications are installed."
    return f"{len(names)} found: " + ", ".join(names)
