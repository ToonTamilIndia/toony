"""Where tools are declared, described to the model, and executed."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from ..log import get

log = get("tools")

Risk = Literal["safe", "sensitive", "dangerous"]


@dataclass
class ToolContext:
    """Everything a tool handler may need, passed as the first argument."""

    config: Any = None
    brain: Any = None
    speak: Callable[[str], None] | None = None
    confirm: Callable[[str], bool] | None = None
    state: dict[str, Any] = field(default_factory=dict)

    def ask(self, question: str) -> bool:
        return bool(self.confirm(question)) if self.confirm else False


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[..., Any]
    risk: Risk = "safe"
    # Tools whose backing command is missing are hidden from the model.
    requires: tuple[str, ...] = ()

    def available(self) -> bool:
        return not self.missing()

    def missing(self) -> list[str]:
        """Unmet requirements. An entry like "wl-copy|xclip" means either will do."""
        from .proc import which
        return [req for req in self.requires
                if not any(which(binary) for binary in req.split("|"))]

    def call(self, ctx: ToolContext, arguments: dict[str, Any]) -> str:
        cleaned = self._clean(arguments)
        result = self.handler(ctx, **cleaned)
        return result if isinstance(result, str) else json.dumps(result, default=str)

    def _clean(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Drop arguments the handler does not accept — models invent extras."""
        signature = inspect.signature(self.handler)
        accepted = {p for p in signature.parameters if p != "ctx"}
        unknown = set(arguments) - accepted
        if unknown:
            log.debug("%s: ignoring unexpected arguments %s", self.name, sorted(unknown))
        return {k: v for k, v in arguments.items() if k in accepted}


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool_obj: Tool) -> None:
        if tool_obj.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool_obj.name}")
        self._tools[tool_obj.name] = tool_obj

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def enabled(self, config) -> list[Tool]:
        """Apply the enabled/disabled lists and drop tools whose binary is absent."""
        allowed = config.get("tools.enabled", ["*"]) or []
        blocked = set(config.get("tools.disabled", []) or [])
        shell_on = bool(config.get("tools.shell.enabled", False))
        out = []
        for t in self.all():
            if t.name in blocked:
                continue
            if "*" not in allowed and t.name not in allowed:
                continue
            if t.name == "run_command" and not shell_on:
                continue
            if not t.available():
                log.debug("tool %s unavailable (missing %s)", t.name, t.missing())
                continue
            out.append(t)
        return out

    def specs(self, config):
        from ..brain.base import ToolSpec
        return [ToolSpec(t.name, t.description, t.schema) for t in self.enabled(config)]


REGISTRY = Registry()


def tool(description: str, risk: Risk = "safe",
         params: dict[str, dict[str, Any]] | None = None,
         required: list[str] | None = None,
         requires: tuple[str, ...] = (),
         name: str | None = None):
    """Register a function as a tool.

    The handler receives a :class:`ToolContext` as ``ctx`` plus the declared
    parameters, and returns a short string that goes back to the model.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        schema = {
            "type": "object",
            "properties": params or {},
            "required": required or [],
            "additionalProperties": False,
        }
        REGISTRY.add(Tool(name=name or func.__name__, description=description,
                          schema=schema, handler=func, risk=risk, requires=requires))
        return func

    return decorator
