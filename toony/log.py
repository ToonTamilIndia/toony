"""Logging setup shared by the daemon and the CLI."""

from __future__ import annotations

import logging
import sys

from .paths import LOG_FILE

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warning": logging.WARNING, "error": logging.ERROR}


def setup(level: str = "info", to_file: bool = True) -> logging.Logger:
    root = logging.getLogger("toony")
    root.setLevel(_LEVELS.get(str(level).lower(), logging.INFO))
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                          datefmt="%H:%M:%S"))
    root.addHandler(stream)

    if to_file:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
            root.addHandler(fh)
        except OSError:
            pass  # a read-only home should not stop the assistant from running
    root.propagate = False
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"toony.{name}")
