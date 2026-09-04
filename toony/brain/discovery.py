"""Working out which models this machine can actually use.

Setting up a voice assistant usually means reading three pages of documentation
to find out that the model named in the config was never pulled. This module
asks instead: which API keys are in the environment, is Ollama running, what
did you pull, and which of those is the best thing to talk to.

Everything here is *probing*, not configuration. It never writes; the callers
(``toony setup``, ``toony models``, and the startup auto-pick) decide what to do
with the answer.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..log import get
from ..net import endpoint_reachable, is_local, online

log = get("brain.discovery")

# Preference among locally installed models, best first. Matched as substrings
# against what Ollama reports, so "qwen2.5:14b" matches the "qwen2.5" entry.
#
# The ordering is about *tool calling*, not benchmark scores. A model that
# writes beautiful prose but cannot emit a function call is useless here, and
# the small Llama and Gemma builds are exactly that. Anything under ~7B is
# ranked below the 7Bs however new it is.
LOCAL_PREFERENCE = (
    "qwen3", "qwen2.5-coder", "qwen2.5", "llama3.3", "llama3.1",
    "mistral-nemo", "mistral-small", "command-r", "hermes3", "firefunction",
    "granite3", "llama3.2", "mistral", "gemma3", "phi4", "phi3", "gemma2",
)

# Models that will not reliably call a tool, whatever else they are good at.
# Kept out of the automatic pick; you can still name one by hand.
NO_TOOL_CALLING = ("gemma:", "gemma2:", "codegemma", "deepseek-coder:",
                   "starcoder", "codellama", "stablelm", "tinyllama",
                   "orca-mini", "vicuna", "wizardlm")

# Sizes, best first. A 14B answers better than a 7B; a 70B will not fit on the
# GPU this is aimed at, and running it on the CPU is a minute per sentence.
SIZE_PREFERENCE = ("14b", "12b", "9b", "8b", "7b", "4b", "3b", "1.5b", "1b")
TOO_BIG = ("70b", "72b", "405b", "235b", "120b", "32b", "27b")

CLOUD_PROVIDERS = {
    "claude": ("ANTHROPIC_API_KEY", "brain.claude"),
    "openai": ("OPENAI_API_KEY", "brain.openai"),
}


@dataclass
class Candidate:
    """One model we could actually use, with why we would."""

    provider: str
    model: str
    reason: str = ""
    local: bool = False
    vision: bool = False
    score: float = 0.0

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class Findings:
    """Everything the probe learned, in one place."""

    online: bool = False
    ollama_up: bool = False
    ollama_models: list[str] = field(default_factory=list)
    keys: dict[str, bool] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def local_only(self) -> list[Candidate]:
        return [c for c in self.candidates if c.local]

    def cloud_only(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.local]


# ---- Ollama ---------------------------------------------------------------
def _api_root(base_url: str) -> str:
    """http://host:11434/v1 -> http://host:11434 — /api/tags is not under /v1."""
    return re.sub(r"/v1/?$", "", (base_url or "").rstrip("/"))


def ollama_models(base_url: str = "http://localhost:11434/v1",
                  timeout: float = 2.0) -> list[str]:
    """Every model pulled on this machine. Empty if Ollama is not running."""
    if not endpoint_reachable(base_url, timeout=min(timeout, 1.0)):
        return []
    url = f"{_api_root(base_url)}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.debug("could not list ollama models: %s", exc)
        return []
    names = [m.get("name", "") for m in payload.get("models", [])]
    return sorted(n for n in names if n)


def ollama_running(base_url: str = "http://localhost:11434/v1") -> bool:
    return endpoint_reachable(base_url, timeout=1.0)


# ---- ranking --------------------------------------------------------------
def _family_rank(name: str) -> int:
    lowered = name.lower()
    for index, family in enumerate(LOCAL_PREFERENCE):
        if family in lowered:
            return index
    return len(LOCAL_PREFERENCE)


def _size_rank(name: str) -> int:
    lowered = name.lower()
    for index, size in enumerate(SIZE_PREFERENCE):
        if size in lowered:
            return index
    return len(SIZE_PREFERENCE)


def can_call_tools(name: str) -> bool:
    lowered = name.lower()
    return not any(bad in lowered for bad in NO_TOOL_CALLING)


def fits_locally(name: str) -> bool:
    """Whether this is small enough to answer in conversational time."""
    lowered = name.lower()
    return not any(big in lowered for big in TOO_BIG)


def score_local(name: str) -> float:
    """Lower is better. Used to sort what Ollama has pulled."""
    score = _family_rank(name) * 10.0 + _size_rank(name)
    if not can_call_tools(name):
        score += 500
    if not fits_locally(name):
        score += 200
    if ":" in name and name.split(":", 1)[1] in ("latest", ""):
        score += 0.5  # a pinned tag is a better bet than :latest
    return score


def rank_local(names) -> list[str]:
    return sorted(names, key=lambda n: (score_local(n), n))


def best_local(names) -> str:
    """The best model to talk to out of what is installed, or ""."""
    usable = [n for n in names if can_call_tools(n) and fits_locally(n)]
    ranked = rank_local(usable or list(names))
    return ranked[0] if ranked else ""


def best_local_vision(names) -> str:
    """The best model that can look at a screenshot, or ""."""
    from .factory import can_see

    seeing = [n for n in names if can_see("ollama", n)]
    return rank_local(seeing)[0] if seeing else ""


# ---- keys -----------------------------------------------------------------
def has_key(cfg, provider: str) -> bool:
    """Whether a usable API key exists for this provider, without printing it."""
    try:
        return bool(cfg.api_key(f"brain.{provider}"))
    except Exception:
        env = CLOUD_PROVIDERS.get(provider, ("", ""))[0]
        return bool(env and os.environ.get(env))


# ---- the whole picture ----------------------------------------------------
def probe(cfg, check_network: bool = True) -> Findings:
    """Look at the machine and report what could be used, best first."""
    found = Findings()
    found.online = online() if check_network else True

    base_url = str(cfg.get("brain.ollama.base_url",
                           "http://localhost:11434/v1"))
    found.ollama_up = ollama_running(base_url)
    if found.ollama_up:
        found.ollama_models = ollama_models(base_url)
        if not found.ollama_models:
            found.notes.append("Ollama is running but has no models. "
                               "Pull one: ollama pull qwen2.5:7b")
    elif is_local(base_url):
        found.notes.append("Ollama is not running. Start it with: ollama serve")

    for provider in CLOUD_PROVIDERS:
        found.keys[provider] = has_key(cfg, provider)

    found.candidates = _candidates(cfg, found)
    if not found.candidates:
        found.notes.append(
            "Nothing to talk to yet. Either install a local model:\n"
            "  ollama pull qwen2.5:7b\n"
            "or set a cloud key:\n"
            "  export ANTHROPIC_API_KEY=...")
    return found


def _candidates(cfg, found: Findings) -> list[Candidate]:
    from .factory import can_see

    out: list[Candidate] = []

    # Cloud first when it is usable: the models are better, and the reason to
    # own a local one is that the cloud is not always there.
    if found.online:
        for provider, (env, section) in CLOUD_PROVIDERS.items():
            if not found.keys.get(provider):
                continue
            model = str(cfg.get(f"{section}.model", "")).strip()
            if not model:
                continue
            out.append(Candidate(provider=provider, model=model, local=False,
                                 vision=can_see(provider, model),
                                 reason=f"{env} is set",
                                 score=0.0 if provider == "claude" else 1.0))

    for name in rank_local(found.ollama_models):
        if not can_call_tools(name):
            continue
        out.append(Candidate(
            provider="ollama", model=name, local=True,
            vision=can_see("ollama", name),
            reason="installed locally", score=10.0 + score_local(name)))

    out.sort(key=lambda c: c.score)
    return out


def summarise(found: Findings) -> str:
    """The human-readable version, for `toony models` and `toony doctor`."""
    lines = [f"network:  {'online' if found.online else 'offline'}"]
    if found.ollama_up:
        count = len(found.ollama_models)
        lines.append(f"ollama:   running, {count} model{'' if count == 1 else 's'}")
    else:
        lines.append("ollama:   not running")
    for provider, present in sorted(found.keys.items()):
        env = CLOUD_PROVIDERS[provider][0]
        lines.append(f"{provider + ':':9} {'key found in ' + env if present else 'no API key'}")
    lines.append("")
    if found.candidates:
        lines.append("Usable, best first:")
        for index, candidate in enumerate(found.candidates[:8]):
            mark = "->" if index == 0 else "  "
            eye = " (can see)" if candidate.vision else ""
            lines.append(f"  {mark} {candidate}{eye} — {candidate.reason}")
    for note in found.notes:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)
