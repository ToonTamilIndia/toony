"""Finding and launching desktop applications."""

from __future__ import annotations

import configparser
import functools
import os
import re
from pathlib import Path

from .proc import CommandError, any_of, spawn, which
from .registry import ToolContext, tool

_XDG_DIRS = [
    Path("~/.local/share/applications").expanduser(),
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("~/.local/share/flatpak/exports/share/applications").expanduser(),
    Path("/var/lib/snapd/desktop/applications"),
]


@functools.lru_cache(maxsize=1)
def _index() -> list[dict[str, str]]:
    """Read every .desktop file once and keep the launchable ones."""
    apps: dict[str, dict[str, str]] = {}
    for directory in _XDG_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.desktop")):
            entry = _parse(path)
            if entry and entry["id"] not in apps:
                apps[entry["id"]] = entry
    return list(apps.values())


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
    return {
        "id": path.stem,
        "name": name,
        "comment": section.get("Comment", "").strip(),
        "exec": re.sub(r"%[fFuUdDnNickvm]", "", section.get("Exec", "")).strip(),
        "keywords": section.get("Keywords", ""),
        "path": str(path),
    }


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
    launcher = any_of("gio", "gtk-launch", "kioclient6", "kioclient")
    if launcher and launcher.endswith("gio"):
        spawn(["gio", "launch", entry["path"]])
    elif launcher and launcher.endswith("gtk-launch"):
        spawn(["gtk-launch", entry["id"]])
    elif entry["exec"]:
        spawn(entry["exec"].split())
    else:
        raise CommandError(f"no way to launch {entry['name']}")
    return f"Opened {entry['name']}."


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
