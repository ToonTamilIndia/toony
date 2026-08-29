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


# ---- running as root ------------------------------------------------------
# Off unless the user turns it on with `toony sudo enable`, and even then only
# commands whose prefix appears in tools.sudo.allowlist are attempted. `sudo -n`
# is used deliberately: the daemon has no terminal, so a command that would ask
# for a password must fail immediately rather than hang holding the assistant.

class SudoUnavailable(CommandError):
    pass


def sudo_enabled(config) -> bool:
    return bool(config and config.get("tools.sudo.enabled", False))


def sudo_allowed(config, argv: Sequence[str]) -> bool:
    """True if this whole command is covered by an allowlist prefix."""
    command = " ".join(argv)
    for entry in (config.get("tools.sudo.allowlist", []) or []):
        prefix = str(entry).strip()
        if not prefix:
            continue
        if command == prefix or command.startswith(prefix + " "):
            return True
    return False


def sudo_ready() -> bool:
    """True if passwordless sudo actually works right now."""
    if not which("sudo"):
        return False
    try:
        proc = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def sudo_run(config, argv: Sequence[str], timeout: float | None = None) -> str:
    """Run one allowlisted command as root. Raises rather than prompting."""
    argv = list(argv)
    if not sudo_enabled(config):
        raise SudoUnavailable(
            "Administrator access is switched off. Turn it on with: toony sudo enable")
    if not sudo_allowed(config, argv):
        raise SudoUnavailable(
            f"'{' '.join(argv)}' is not in the administrator allowlist. "
            "Add it with: toony sudo allow \"<command prefix>\"")
    if not which("sudo"):
        raise SudoUnavailable("sudo is not installed")
    if timeout is None:
        timeout = float(config.get("tools.sudo.timeout_s", 60))
    log.info("sudo %s", " ".join(argv))
    try:
        proc = subprocess.run(["sudo", "-n", *argv], capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{argv[0]} timed out after {timeout:g}s") from exc
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        if "password is required" in err or "a terminal is required" in err:
            raise SudoUnavailable(
                "Passwordless sudo is not set up, so I cannot run that as "
                "administrator. Run: toony sudo enable")
        raise CommandError(err or f"{argv[0]} exited with {proc.returncode}")
    return proc.stdout.decode("utf-8", "replace").strip()
