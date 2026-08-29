"""Where tools are declared, described to the model, and executed."""

from __future__ import annotations

import difflib
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
        """Make a model's arguments usable: drop extras, fix types, fix names.

        Small models get tool calls almost right — an extra argument, "5"
        instead of 5, `filename` instead of `path`. Every one of those is a
        failed turn if it is passed straight through, and a trivial fix here.
        """
        signature = inspect.signature(self.handler)
        accepted = {p for p in signature.parameters if p != "ctx"}
        out: dict[str, Any] = {}
        for key, value in (arguments or {}).items():
            name = key if key in accepted else self._nearest(key, accepted)
            if name is None:
                log.debug("%s: dropping unexpected argument %r", self.name, key)
                continue
            if name != key:
                log.info("%s: reading %r as %r", self.name, key, name)
            out[name] = self._coerce(name, value)
        return out

    @staticmethod
    def _nearest(key: str, accepted: set[str]) -> str | None:
        matches = difflib.get_close_matches(key, sorted(accepted), n=1, cutoff=0.75)
        return matches[0] if matches else None

    def _coerce(self, name: str, value: Any) -> Any:
        """JSON schemas are advisory to a small model; the types arrive wrong."""
        spec = self.schema.get("properties", {}).get(name, {})
        wanted = spec.get("type")
        if wanted == "integer" and isinstance(value, str):
            try:
                return int(float(value.strip()))
            except ValueError:
                return value
        if wanted == "number" and isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return value
        if wanted == "boolean" and isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "yes", "1", "on"):
                return True
            if lowered in ("false", "no", "0", "off"):
                return False
        if wanted == "string" and isinstance(value, (int, float, bool)):
            return str(value)
        return value


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool_obj: Tool) -> None:
        if tool_obj.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool_obj.name}")
        self._tools[tool_obj.name] = tool_obj

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def resolve(self, name: str, config=None) -> Tool | None:
        """Find the tool the model meant, not only the one it named.

        Models invent near-misses — `get_time`, `open_app`, `system_info` — and
        each one is otherwise a wasted round trip. But a wrong guess is worse
        than none, so: the match must be unambiguous, it must share the verb or
        contain the name, and it is never allowed to land on a dangerous tool.
        Guessing that `power_of` meant `power_off` is not a service.
        """
        exact = self._tools.get(name)
        if exact is not None:
            return exact
        pool = [t for t in (self.enabled(config) if config else self.all())
                if t.risk != "dangerous"]
        if not pool:
            return None

        stem = name.replace("_", "").lower()
        contains = [t for t in pool
                    if stem and (stem in t.name.replace("_", "")
                                 or t.name.replace("_", "") in stem)]
        if len(contains) == 1:
            return self._matched(name, contains[0])

        verb = name.split("_")[0]
        same_verb = [t.name for t in pool if t.name.split("_")[0] == verb]
        close = difflib.get_close_matches(name, same_verb, n=1, cutoff=0.72)
        if close:
            return self._matched(name, self._tools[close[0]])

        if contains:
            log.info("%r is ambiguous between %s — not guessing", name,
                     ", ".join(t.name for t in contains[:4]))
        return None

    @staticmethod
    def _matched(asked: str, tool_obj: "Tool") -> "Tool":
        log.info("model asked for %r — using %r", asked, tool_obj.name)
        return tool_obj

    def suggest(self, name: str, config=None, limit: int = 4) -> list[str]:
        pool = [t.name for t in (self.enabled(config) if config else self.all())]
        close = difflib.get_close_matches(name, pool, n=limit, cutoff=0.4)
        return close or sorted(pool)[:limit]

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
