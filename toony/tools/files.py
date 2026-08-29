"""Finding and opening files in the user's home directory."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .proc import CommandError, any_of, run, spawn, which
from .registry import ToolContext, tool

_HOME = Path.home()
_SKIP = {".cache", ".git", "node_modules", "__pycache__", ".venv", "venv",
         ".local/share/Trash", "snap", ".steam"}


def _safe(path: Path) -> Path:
    """Keep file tools inside the home directory."""
    resolved = Path(os.path.expanduser(str(path))).resolve()
    if _HOME not in resolved.parents and resolved != _HOME:
        raise CommandError(f"{resolved} is outside your home directory")
    return resolved


@tool(description="Search for files by name under the user's home directory.",
      params={"query": {"type": "string", "description": "Part of the file name."},
              "directory": {"type": "string",
                            "description": "Where to look. Defaults to the home directory."},
              "limit": {"type": "integer", "default": 10}},
      required=["query"])
def find_files(ctx: ToolContext, query: str, directory: str = "~",
               limit: int = 10) -> str:
    limit = max(1, min(50, int(limit)))
    root = _safe(Path(directory))
    # plocate is instant when it is present; otherwise walk, bounded.
    if which("plocate") and str(root) == str(_HOME):
        try:
            lines = run(["plocate", "-i", "-l", str(limit), query],
                        check=False, timeout=8).splitlines()
            hits = [line for line in lines if line.startswith(str(_HOME))][:limit]
            if hits:
                return _format(hits)
        except CommandError:
            pass
    hits = []
    needle = query.lower()
    deadline = time.monotonic() + 8
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() > deadline:
            break
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in _SKIP]
        for filename in filenames:
            if needle in filename.lower():
                hits.append(os.path.join(dirpath, filename))
                if len(hits) >= limit:
                    return _format(hits)
    return _format(hits)


def _format(hits: list[str]) -> str:
    if not hits:
        return "I found no matching files."
    lines = []
    for path in hits:
        try:
            size = os.path.getsize(path)
            when = time.strftime("%d %b", time.localtime(os.path.getmtime(path)))
            lines.append(f"{path} ({size / 1024:.0f} KB, modified {when})")
        except OSError:
            lines.append(path)
    return f"{len(lines)} matches:\n" + "\n".join(lines)


@tool(description="Open a file or folder in its default application.",
      risk="sensitive", params={"path": {"type": "string"}}, required=["path"])
def open_file(ctx: ToolContext, path: str) -> str:
    target = _safe(Path(path))
    if not target.exists():
        return f"There is no file at {target}."
    opener = any_of("xdg-open", "kde-open6", "kde-open", "gio")
    if not opener:
        raise CommandError("no desktop file opener found")
    spawn([opener, "open", str(target)] if opener.endswith("gio")
          else [opener, str(target)])
    return f"Opened {target.name}."


@tool(description="Read a short text file so you can answer questions about it. "
                  "Only works on text files under the home directory.",
      risk="sensitive",
      params={"path": {"type": "string"},
              "max_chars": {"type": "integer", "default": 4000}},
      required=["path"])
def read_text_file(ctx: ToolContext, path: str, max_chars: int = 4000) -> str:
    target = _safe(Path(path))
    if not target.is_file():
        return f"There is no file at {target}."
    if target.stat().st_size > 5_000_000:
        return "That file is too large for me to read aloud."
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CommandError(str(exc)) from exc
    limit = max(200, min(20000, int(max_chars)))
    if len(text) > limit:
        return text[:limit] + f"\n[truncated, {len(text)} characters total]"
    return text
