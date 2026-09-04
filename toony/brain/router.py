"""Falling back from one model to another, without you having to notice.

The reason to pick Claude and *also* keep a 7B on the GPU is that the good one
is not always there. Trains lose signal, hotel wifi dies, a key hits its rate
limit at the worst moment. What should not happen is the assistant answering
"I could not reach the model" when there is a perfectly good model running on
localhost.

:class:`RoutingBrain` is a :class:`~toony.brain.base.Brain` that holds several
of them in preference order and uses the first one that works. A route that
fails goes into a cooldown rather than being dropped, so the moment the network
comes back the good model is used again with no restart and no configuration.

Two rules keep this from being dangerous rather than helpful:

**Only transport failures fail over.** A refusal, a bad tool call, a model that
answers badly — those are answers, and trying a different model would hide a
real problem. Connection errors, timeouts, rate limits and 5xx fail over;
:class:`~toony.brain.base.InvalidRequest` never does, because the transcript
would be just as invalid for the next backend.

**A stream that has already spoken cannot be retried.** Once a word has reached
the speakers, switching backends would repeat it. So failover during streaming
happens only before the first token.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..log import get
from ..net import NETWORK, is_local
from .base import (Brain, BrainError, BrainReply, InvalidRequest, Message,
                   ToolSpec)

log = get("brain.router")

# Exception class names that mean "the request never got a fair hearing".
# Matched by name so this module does not import the openai or anthropic
# packages, neither of which is guaranteed to be installed.
TRANSPORT_ERRORS = (
    "APIConnectionError", "APITimeoutError", "APIConnectionTimeoutError",
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
    "RateLimitError", "InternalServerError", "APIStatusError",
    "ServiceUnavailable", "OverloadedError", "AuthenticationError",
    "PermissionDeniedError", "NotFoundError", "socket.timeout", "OSError",
    "URLError", "RemoteProtocolError", "RemoteDisconnected",
)

# Substrings in the message, for backends that raise a plain BrainError.
TRANSPORT_PHRASES = (
    "could not reach", "connection", "connect", "timed out", "timeout",
    "rate limit", "overloaded", "temporarily unavailable", "503", "502",
    "504", "529", "no route to host", "network is unreachable",
    "name or service not known", "unauthorized", "invalid api key",
    "authentication", "insufficient_quota", "quota", "model not found",
    "is not installed", "try pulling it",
)


def is_transport_failure(exc: BaseException) -> bool:
    """Whether this failure is worth trying a different backend for."""
    if isinstance(exc, InvalidRequest):
        return False
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in TRANSPORT_ERRORS:
            return True
        current = current.__cause__ or current.__context__
    message = str(exc).lower()
    return any(phrase in message for phrase in TRANSPORT_PHRASES)


@dataclass
class Route:
    """One backend we are willing to use, built the first time it is needed."""

    provider: str
    model: str
    build: Callable[[], Brain]
    local: bool = False
    _brain: Brain | None = field(default=None, repr=False)
    down_until: float = 0.0
    last_error: str = ""
    failures: int = 0
    uses: int = 0

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else self.provider

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.down_until

    def brain(self) -> Brain:
        if self._brain is None:
            self._brain = self.build()
        return self._brain

    def mark_down(self, error: str, cooldown: float) -> None:
        self.failures += 1
        self.last_error = error
        # Back off harder each time, so a provider that is properly gone is not
        # retried on every single turn.
        self.down_until = time.monotonic() + min(cooldown * self.failures,
                                                 cooldown * 6)

    def mark_up(self) -> None:
        self.failures = 0
        self.last_error = ""
        self.down_until = 0.0

    def close(self) -> None:
        if self._brain is not None:
            try:
                self._brain.close()
            except Exception:
                log.debug("closing %s failed", self.label, exc_info=True)
            self._brain = None


class RoutingBrain(Brain):
    """Several brains, tried in order, with the failed ones rested."""

    def __init__(self, routes: list[Route], cooldown: float = 90.0,
                 announce: Callable[[str], None] | None = None,
                 skip_remote_when_offline: bool = True):
        if not routes:
            raise BrainError("No model backends are configured.")
        self.routes = routes
        self.cooldown = max(5.0, float(cooldown))
        self.announce = announce
        self.skip_remote_when_offline = skip_remote_when_offline
        self._lock = threading.Lock()
        self._current: Route | None = None
        self.name = routes[0].label
        self.switches = 0

    # ---- which one to use -------------------------------------------------
    @property
    def primary(self) -> Route:
        return self.routes[0]

    @property
    def current(self) -> Route:
        return self._current or self.routes[0]

    def usable(self) -> list[Route]:
        """Routes worth trying right now, in preference order."""
        offline = (self.skip_remote_when_offline
                   and any(r.local for r in self.routes)
                   and NETWORK.offline())
        ready = [r for r in self.routes
                 if r.available and not (offline and not r.local)]
        if ready:
            return ready
        # Everything is resting or unreachable. Rather than refuse outright,
        # try the local ones anyway — a stale cooldown is not a good reason to
        # say nothing.
        local = [r for r in self.routes if r.local]
        return local or list(self.routes)

    def _announce_switch(self, route: Route, previous: Route | None) -> None:
        """Say which backend answered, when it is not the one you would expect.

        ``previous`` is None on the very first turn — which is precisely when
        this matters most, because "Claude is unreachable, the local model
        answered" is the difference between a working assistant and a broken
        one. So the primary stands in for "what you were expecting".
        """
        expected = previous or self.primary
        if route is expected:
            return
        self.switches += 1
        self.name = route.label
        if route is self.primary:
            message = f"{expected.label} is answering again"
        elif route.local:
            message = (f"{expected.label} is not reachable — using "
                       f"{route.label} on this machine instead")
        else:
            message = f"{expected.label} gave way to {route.label}"
        log.warning("%s", message)
        if self.announce:
            try:
                self.announce(message)
            except Exception:
                log.debug("fallback announcement failed", exc_info=True)

    # ---- the Brain surface ------------------------------------------------
    def reply(self, system: str, messages: list[Message],
              tools: list[ToolSpec]) -> BrainReply:
        return self._attempt(
            lambda brain: brain.reply(system, messages, tools), streaming=False)

    def stream_reply(self, system: str, messages: list[Message],
                     tools: list[ToolSpec],
                     on_text: Callable[[str], None] | None = None) -> BrainReply:
        # Text already spoken cannot be unspoken. The guard hands each attempt
        # its own gate: once one of them emits, that attempt owns the turn and
        # a later failure is raised rather than retried elsewhere.
        def run(brain: Brain, emitted: list[bool]) -> BrainReply:
            def forward(chunk: str) -> None:
                if chunk:
                    emitted.append(True)
                if on_text:
                    on_text(chunk)

            return brain.stream_reply(system, messages, tools, forward)

        return self._attempt(run, streaming=True)

    def _attempt(self, call, streaming: bool) -> BrainReply:
        previous = self._current
        errors: list[str] = []
        candidates = self.usable()

        for route in candidates:
            emitted: list[bool] = []
            try:
                brain = route.brain()
            except BrainError as exc:
                # The backend could not even be constructed — a missing
                # package, a missing key. That is permanent until the config
                # changes, so rest it for a good while.
                route.mark_down(str(exc), self.cooldown * 4)
                errors.append(f"{route.label}: {exc}")
                continue

            try:
                result = call(brain, emitted) if streaming else call(brain)
            except InvalidRequest:
                # The transcript is the problem, not this backend. The agent
                # knows how to recover from it; another model would only fail
                # the same way.
                with self._lock:
                    self._current = route
                raise
            except Exception as exc:
                if emitted:
                    log.error("%s failed after it had started speaking — not "
                              "retrying elsewhere: %s", route.label, exc)
                    route.mark_down(str(exc), self.cooldown)
                    raise
                if not is_transport_failure(exc):
                    raise
                route.mark_down(str(exc), self.cooldown)
                if not route.local:
                    NETWORK.note_failure()
                errors.append(f"{route.label}: {exc}")
                log.warning("%s is not answering (%s) — trying the next one",
                            route.label, exc)
                continue

            route.mark_up()
            route.uses += 1
            if not route.local:
                NETWORK.note_success()
            with self._lock:
                self._current = route
                self.name = route.label
            self._announce_switch(route, previous)
            return result

        detail = "; ".join(errors) or "no backend was available"
        raise BrainError(_explain(detail, candidates))

    # ---- reporting --------------------------------------------------------
    def check(self) -> str:
        lines = []
        for route in self.routes:
            state = "ready" if route.available else (
                f"resting {int(route.down_until - time.monotonic())}s "
                f"({route.last_error[:60]})")
            here = " <- in use" if route is self._current else ""
            lines.append(f"  {route.label}: {state}{here}")
        return "\n".join(lines) or "no routes"

    def status(self) -> dict:
        return {
            "current": self.current.label,
            "primary": self.primary.label,
            "switches": self.switches,
            "online": NETWORK.online(),
            "routes": [{"label": r.label, "local": r.local,
                        "available": r.available, "uses": r.uses,
                        "failures": r.failures, "error": r.last_error}
                       for r in self.routes],
        }

    def close(self) -> None:
        for route in self.routes:
            route.close()


def _explain(detail: str, candidates: list[Route]) -> str:
    # Not a fresh probe: every remote route has just failed, which is better
    # evidence about the network than another handshake would be.
    if all(not r.local for r in candidates) and _looks_offline(detail):
        return ("There is no network and no local model to fall back to. "
                "Install one with: ollama pull qwen2.5:7b")
    return f"No model could answer. Tried: {detail}"


def _looks_offline(detail: str) -> bool:
    lowered = detail.lower()
    return any(phrase in lowered for phrase in
               ("could not reach", "connection", "timed out", "unreachable",
                "no route", "name or service"))


# ---- building it from configuration ---------------------------------------
def build_routes(cfg, announce: Callable[[str], None] | None = None) -> list[Route]:
    """The preference order: what you chose, then whatever else would work.

    ``brain.fallback`` decides how far to go:

    ``off``    only the configured provider — the old behaviour.
    ``auto``   the configured provider, then anything else usable, local last.
    a list     exactly these providers, in this order.
    """
    from . import factory

    chosen = str(cfg.get("brain.provider", "ollama")).lower()
    setting = cfg.get("brain.fallback", "auto")
    if isinstance(setting, str):
        setting = setting.strip().lower()

    order: list[str]
    if setting in ("off", "none", "false", False):
        order = [chosen]
    elif isinstance(setting, (list, tuple)):
        order = [str(p).lower() for p in setting if str(p).strip()]
        if chosen not in order:
            order.insert(0, chosen)
    else:
        order = [chosen] + [p for p in factory.PROVIDERS if p != chosen]

    routes: list[Route] = []
    for provider in order:
        if provider not in factory.PROVIDERS:
            log.warning("ignoring unknown fallback provider %r", provider)
            continue
        if any(r.provider == provider for r in routes):
            continue
        model = resolve_model(cfg, provider)
        if not model:
            continue
        if provider != chosen and not _plausible(cfg, provider, model):
            continue
        base_url = str(cfg.get(f"brain.{provider}.base_url", ""))
        routes.append(Route(
            provider=provider, model=model, local=is_local(base_url) if base_url else False,
            build=_builder(cfg, provider, model)))
    return routes


def _builder(cfg, provider: str, model: str) -> Callable[[], Brain]:
    def build() -> Brain:
        from . import factory

        return factory.build_one(cfg, provider, model)

    return build


def _plausible(cfg, provider: str, model: str) -> bool:
    """Is it even worth listing this as a fallback?

    A provider with no key will never answer, and putting it in the chain just
    means every failover pauses on a guaranteed 401.
    """
    from .discovery import has_key

    if provider == "ollama":
        return True
    return has_key(cfg, provider)


def resolve_model(cfg, provider: str) -> str:
    """The model to use for a provider — the configured one, or a better guess.

    With ``brain.auto_model`` on, a local model named in the config but never
    pulled is replaced by the best one that *was* pulled, rather than failing
    with "model not found" every single turn.
    """
    model = str(cfg.get(f"brain.{provider}.model", "")).strip()
    if provider != "ollama" or not cfg.get("brain.auto_model", True):
        return model

    from .discovery import best_local, ollama_models

    installed = ollama_models(str(cfg.get("brain.ollama.base_url",
                                          "http://localhost:11434/v1")))
    if not installed:
        return model
    if model and _installed(model, installed):
        return model
    picked = best_local(installed)
    if picked and picked != model:
        log.info("%s is not installed — using %s instead", model or "(no model)",
                 picked)
    return picked or model


def _installed(model: str, installed: list[str]) -> bool:
    """Whether Ollama has this model, allowing for the implicit :latest tag."""
    if model in installed or f"{model}:latest" in installed:
        return True
    # "qwen2.5" written without a tag matches any tag of it.
    return ":" not in model and any(name.split(":", 1)[0] == model
                                    for name in installed)


def build(cfg, announce: Callable[[str], None] | None = None) -> Brain:
    """The brain the assistant actually talks to."""
    from . import factory

    routes = build_routes(cfg, announce)
    if not routes:
        return factory.build_brain(cfg)
    if len(routes) == 1:
        # Nothing to route between. Return the plain backend so there is no
        # extra layer in the stack traces.
        return routes[0].brain()
    log.info("model order: %s", " -> ".join(r.label for r in routes))
    return RoutingBrain(
        routes,
        cooldown=float(cfg.get("brain.fallback_cooldown_s", 90)),
        announce=announce if cfg.get("brain.announce_fallback", True) else None)
