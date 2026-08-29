"""The gate between "the model wants to do X" and X actually happening.

Every tool carries a risk class. Configuration maps each class to allow, ask or
deny. Nothing reaches the desktop without passing through :func:`authorise`.
"""

from __future__ import annotations

from typing import Literal

from .log import get
from .tools.registry import Tool, ToolContext

log = get("safety")

Decision = Literal["allow", "ask", "deny"]

_POLICY_KEY = {"safe": "tools.policy_safe",
               "sensitive": "tools.policy_sensitive",
               "dangerous": "tools.policy_dangerous"}


def policy_for(config, risk: str) -> Decision:
    value = str(config.get(_POLICY_KEY.get(risk, "tools.policy_dangerous"), "ask"))
    return value if value in ("allow", "ask", "deny") else "ask"


def describe(tool: Tool, arguments: dict) -> str:
    """A short spoken sentence asking permission."""
    if arguments:
        detail = ", ".join(f"{k} {_short(v)}" for k, v in arguments.items())
        return f"Can I {_phrase(tool.name)} with {detail}?"
    return f"Can I {_phrase(tool.name)}?"


def _phrase(name: str) -> str:
    return name.replace("_", " ")


def _short(value, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


class Denied(Exception):
    """The user (or policy) refused a tool call."""


def authorise(tool: Tool, arguments: dict, ctx: ToolContext) -> None:
    """Raise :class:`Denied` unless this call is permitted."""
    config = ctx.config
    decision = policy_for(config, tool.risk) if config else "ask"

    if decision == "allow":
        return
    if decision == "deny":
        log.info("denied %s by policy (%s)", tool.name, tool.risk)
        raise Denied(f"I am not allowed to {_phrase(tool.name)}. "
                     f"The policy for {tool.risk} actions is set to deny.")

    question = describe(tool, arguments)
    if ctx.ask(question):
        log.info("approved %s", tool.name)
        return
    log.info("user declined %s", tool.name)
    raise Denied("You declined that, so I did not do it.")


def execute(tool: Tool, arguments: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Authorise then run a tool. Returns (result_text, is_error)."""
    from .tools.proc import CommandError

    try:
        authorise(tool, arguments, ctx)
    except Denied as exc:
        return str(exc), True

    try:
        return tool.call(ctx, arguments), False
    except CommandError as exc:
        log.warning("%s failed: %s", tool.name, exc)
        return f"The {_phrase(tool.name)} action failed: {exc}", True
    except TypeError as exc:
        log.warning("%s called with bad arguments: %s", tool.name, exc)
        return f"I called {tool.name} incorrectly: {exc}", True
    except Exception as exc:  # a tool must never take the daemon down
        log.exception("%s raised", tool.name)
        return f"{tool.name} raised an unexpected {exc.__class__.__name__}: {exc}", True
