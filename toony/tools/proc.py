"""Subprocess helpers. Every external command Toony runs goes through here."""

from __future__ import annotations

import functools
import shutil
import subprocess
from typing import Sequence

from ..log import get

log = get("tools.proc")


@functools.lru_cache(maxsize=256)
def which(binary: str) -> str | None:
    return shutil.which(binary)


def any_of(*binaries: str) -> str | None:
    """First of several interchangeable tools that is actually installed."""
    for binary in binaries:
        found = which(binary)
        if found:
            return found
    return None


class CommandError(RuntimeError):
    pass


def run(argv: Sequence[str], timeout: float = 10.0, check: bool = True,
        stdin: bytes | None = None) -> str:
    """Run a command with no shell involved and return its stdout."""
    log.debug("run %s", " ".join(argv))
    try:
        proc = subprocess.run(list(argv), capture_output=True, timeout=timeout,
                              input=stdin)
    except FileNotFoundError as exc:
        raise CommandError(f"{argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{argv[0]} timed out after {timeout:g}s") from exc
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if check and proc.returncode != 0:
        raise CommandError(err or out or f"{argv[0]} exited with {proc.returncode}")
    return out


def spawn(argv: Sequence[str]) -> None:
    """Start a detached GUI process that outlives the daemon."""
    log.debug("spawn %s", " ".join(argv))
    try:
        subprocess.Popen(list(argv), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise CommandError(f"{argv[0]} is not installed") from exc
