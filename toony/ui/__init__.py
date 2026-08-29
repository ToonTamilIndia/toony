"""The Toony window.

Qt is imported lazily inside :func:`run` so that the rest of Toony — the
daemon, the CLI, the tests — keeps working on a machine with no PySide6 and no
display at all.
"""

from __future__ import annotations


def run(start_hidden: bool | None = None) -> int:
    from .main import run as _run

    return _run(start_hidden)


def available() -> bool:
    """True if PySide6 is importable, without importing the whole GUI."""
    import importlib.util
    import sys

    if "PySide6" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("PySide6") is not None
    except (ImportError, ValueError):
        return False


__all__ = ["run", "available"]
