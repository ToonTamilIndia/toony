"""Filesystem locations Toony uses, following the XDG base directory spec."""

from __future__ import annotations

import os
from pathlib import Path

from . import APP_NAME


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else Path(default).expanduser()


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", "~/.config") / APP_NAME
DATA_DIR = _xdg("XDG_DATA_HOME", "~/.local/share") / APP_NAME
CACHE_DIR = _xdg("XDG_CACHE_HOME", "~/.cache") / APP_NAME
STATE_DIR = _xdg("XDG_STATE_HOME", "~/.local/state") / APP_NAME

CONFIG_FILE = CONFIG_DIR / "config.toml"
MEMORY_FILE = DATA_DIR / "memory.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
LOG_FILE = STATE_DIR / "toony.log"
PIPER_DIR = DATA_DIR / "piper"
WAKEWORD_DIR = DATA_DIR / "wakeword"
SCREENSHOT_DIR = CACHE_DIR / "screenshots"
# One JSON file per conversation, so a corrupt one cannot take the rest with it.
CONVERSATION_DIR = DATA_DIR / "conversations"
AVATAR_FILE = CACHE_DIR / "avatar.png"

APPLICATIONS_DIR = _xdg("XDG_DATA_HOME", "~/.local/share") / "applications"
AUTOSTART_DIR = _xdg("XDG_CONFIG_HOME", "~/.config") / "autostart"
ICON_DIR = (_xdg("XDG_DATA_HOME", "~/.local/share")
            / "icons" / "hicolor" / "256x256" / "apps")


def runtime_dir() -> Path:
    """Where the control socket lives. Falls back to /tmp on odd sessions."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and Path(base).is_dir():
        return Path(base) / APP_NAME
    return Path(f"/tmp/{APP_NAME}-{os.getuid()}")


def socket_path() -> Path:
    return runtime_dir() / "control.sock"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, CACHE_DIR, STATE_DIR, PIPER_DIR,
              WAKEWORD_DIR, SCREENSHOT_DIR, CONVERSATION_DIR, runtime_dir()):
        d.mkdir(parents=True, exist_ok=True)
