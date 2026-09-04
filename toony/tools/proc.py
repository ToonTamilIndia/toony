"""Subprocess helpers. Every external command Toony runs goes through here."""

from __future__ import annotations

import functools
import shutil
import subprocess
from typing import Sequence

from ..log import get

log = get("tools.proc")


@functools.lru_cache(maxsize=512)
def _which_cached(binary: str) -> str | None:
    return shutil.which(binary)


def which(binary: str) -> str | None:
    """Where a program is, or None.

    Cached, because this is on the hot path. Every turn asks every tool whether
    its backing command exists, and an uncached ``shutil.which`` stats every
    directory on PATH for every one of them — several thousand syscalls before
    the model has been sent anything. Programs do not appear and disappear
    during a conversation; :func:`forget_which` is there for when they do.
    """
    return _which_cached(binary)


def forget_which() -> None:
    """Re-check the filesystem — after an install, or on `toony reload`."""
    _which_cached.cache_clear()


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


# ---- launching desktop applications ---------------------------------------
# Two things went wrong every time Toony was asked to open Firefox, and both
# were invisible.
#
# The first is systemd. The daemon runs as a user service, so anything it forks
# lands in toony.service's control group, and the default KillMode takes that
# whole group down together: `systemctl restart toony`, or a single crash with
# Restart=on-failure, closed every application Toony had opened. Starting the
# app inside its own transient scope is what cuts that tie — start_new_session
# only leaves the session, not the cgroup.
#
# The second is that failure was thrown away. Popen with stderr=DEVNULL cannot
# tell "launched" from "refused", so a launcher that exited 1 still produced a
# cheerful "Opened Firefox." — and the model, believing it, moved on.

#: None until the question has been asked once; then True or False for good.
_SCOPE: bool | None = None


def _scope_supported() -> bool:
    """True if systemd can give a launched app a control group of its own."""
    global _SCOPE
    if _SCOPE is None:
        _SCOPE = False
        if which("systemd-run"):
            try:
                proc = subprocess.run(
                    ["systemd-run", "--user", "--scope", "--quiet",
                     "--collect", "true"], capture_output=True, timeout=8)
                _SCOPE = proc.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                _SCOPE = False
            log.debug("transient scopes %s",
                      "available" if _SCOPE else "unavailable")
    return _SCOPE


def spawn(argv: Sequence[str]) -> None:
    """Start a detached GUI process that outlives the daemon.

    Fire and forget: use :func:`launch` for anything whose success is worth
    reporting back to the user.
    """
    log.debug("spawn %s", " ".join(argv))
    try:
        subprocess.Popen(list(argv), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise CommandError(f"{argv[0]} is not installed") from exc


def launch(argv: Sequence[str], what: str = "", settle: float = 1.2) -> None:
    """Start an application, outside the daemon's cgroup, and check it started.

    Raises :class:`CommandError` with whatever the launcher actually said, so
    "I opened it" is only ever said when something opened.
    """
    global _SCOPE
    import tempfile

    argv = [str(a) for a in argv]
    what = what or argv[0]
    wrapped = (["systemd-run", "--user", "--scope", "--quiet", "--collect",
                *argv] if _scope_supported() else argv)
    log.debug("launch %s", " ".join(wrapped))

    # A file rather than a pipe: the process usually outlives this call, and a
    # pipe nobody drains eventually blocks the application we just started.
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(wrapped, start_new_session=True,
                                    stdout=subprocess.DEVNULL, stderr=errors,
                                    stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise CommandError(f"{argv[0]} is not installed") from exc
        except OSError as exc:
            raise CommandError(f"could not start {what}: {exc}") from exc

        try:
            code = proc.wait(timeout=settle)
        except subprocess.TimeoutExpired:
            return                      # still running after a moment: it is up
        if code == 0:
            return                      # a launcher that handed off and left
        errors.seek(0)
        message = errors.read().decode("utf-8", "replace").strip()

    if wrapped is not argv:
        # systemd refused the scope rather than the application refusing to
        # start. Try again without it: a stale D-Bus session is not a reason
        # to tell the user their browser is broken.
        log.info("could not run %s in its own scope (%s) — retrying plainly",
                 what, message.splitlines()[0] if message else code)
        _SCOPE = False
        return launch(argv, what, settle)
    raise CommandError(message.splitlines()[-1] if message
                       else f"{what} exited with {code}")


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
