"""Running shell commands — off by default, allowlisted when on."""

from __future__ import annotations

import shlex

from .proc import CommandError, run
from .registry import ToolContext, tool

_MAX_OUTPUT = 2000


@tool(description="Run a read-only shell command on this machine and return its "
                  "output. Only a small allowlist of commands is permitted.",
      risk="dangerous",
      params={"command": {"type": "string",
                          "description": "The command line to run."}},
      required=["command"])
def run_command(ctx: ToolContext, command: str) -> str:
    config = ctx.config
    if config is None or not config.get("tools.shell.enabled", False):
        return "Running shell commands is disabled in my configuration."

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"I could not parse that command: {exc}"
    if not argv:
        return "That was an empty command."

    # Reject anything that would let the model escape the allowlist.
    for token in ("|", ";", "&&", "||", ">", ">>", "<", "`", "$("):
        if token in command:
            return f"I will not run a command containing {token!r}."

    allowlist = [str(entry) for entry in config.get("tools.shell.allowlist", [])]
    if not _allowed(command, argv, allowlist):
        return (f"{argv[0]} is not on my allowlist. "
                "The user can add it with: toony config set tools.shell.allowlist ...")

    timeout = float(config.get("tools.shell.timeout_s", 15))
    try:
        output = run(argv, timeout=timeout, check=False)
    except CommandError as exc:
        return f"The command failed: {exc}"
    if not output:
        return "The command produced no output."
    if len(output) > _MAX_OUTPUT:
        return output[:_MAX_OUTPUT] + "\n[output truncated]"
    return output


def _allowed(command: str, argv: list[str], allowlist: list[str]) -> bool:
    """An entry may be a bare binary ("df") or a prefix ("systemctl status")."""
    normalised = " ".join(argv)
    for entry in allowlist:
        entry = entry.strip()
        if not entry:
            continue
        if " " in entry:
            if normalised == entry or normalised.startswith(entry + " "):
                return True
        elif argv[0] == entry:
            return True
    return False
