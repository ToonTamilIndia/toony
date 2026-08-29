"""Configuration: defaults, TOML persistence, and dotted-key access.

The whole assistant is configured from one file (``~/.config/toony/config.toml``).
Anything absent from that file falls back to :data:`DEFAULTS`, so the file only
ever needs to hold the keys you actually changed.
"""

from __future__ import annotations

import copy
import os
import tomllib
from typing import Any

from .paths import CONFIG_FILE, PIPER_DIR, WAKEWORD_DIR

DEFAULTS: dict[str, Any] = {
    "general": {
        "name": "Toony",
        "language": "en",
        "log_level": "info",
        # Spoken replies are capped so the assistant stays conversational.
        "reply_word_target": 60,
    },
    "brain": {
        # claude | openai | ollama
        "provider": "ollama",
        "temperature": 0.5,
        "max_history_turns": 20,
        "max_tool_iterations": 6,
        "system_prompt": "",  # empty -> built-in prompt from brain/prompts.py
    },
    "brain.claude": {
        "model": "claude-opus-5",
        "api_key": "",                      # prefer the env var below
        "api_key_env": "ANTHROPIC_API_KEY",
        "max_tokens": 16000,
        # low | medium | high | xhigh | max — "low" keeps voice latency down.
        "effort": "low",
        "thinking": "adaptive",             # adaptive | disabled
        # Server-side refusal fallback: on a policy decline the API retries the
        # same request on a fallback model inside the same call.
        "refusal_fallback": True,
    },
    "brain.openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key": "",
        "api_key_env": "OPENAI_API_KEY",
        "max_tokens": 2048,
    },
    "brain.ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3:4b",
        "api_key": "ollama",                # Ollama ignores it, the client needs one
        "api_key_env": "",
        "max_tokens": 2048,
    },
    "stt": {
        # local | openai
        "provider": "local",
        "initial_prompt": "",               # bias the decoder toward your vocabulary
    },
    "stt.local": {
        "model": "small",                   # tiny | base | small | medium | large-v3
        "device": "auto",                   # auto | cuda | cpu
        "compute_type": "auto",             # auto | float16 | int8_float16 | int8
        "beam_size": 1,
    },
    "stt.openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "whisper-1",
        "api_key": "",
        "api_key_env": "OPENAI_API_KEY",
    },
    "tts": {
        # piper | openai | espeak
        "provider": "piper",
        "speed": 1.0,
        # Speak the reply while it is still being generated.
        "stream": True,
    },
    "tts.piper": {
        "voice": "en_US-amy-medium",
        "model_dir": str(PIPER_DIR),
        "binary": "piper",
    },
    "tts.openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "tts-1",
        "voice": "alloy",
        "api_key": "",
        "api_key_env": "OPENAI_API_KEY",
    },
    "tts.espeak": {
        "voice": "en",
        "words_per_minute": 165,
    },
    "audio": {
        "input_device": "",                 # "" = system default
        "output_device": "",
        "sample_rate": 16000,
        "channels": 1,
        # Silence that ends an utterance, and a hard ceiling on recording.
        "silence_ms": 800,
        "max_utterance_s": 30,
        "min_utterance_ms": 350,
        "vad": "energy",                    # energy | webrtc
        "vad_aggressiveness": 2,            # webrtc only, 0-3
        "energy_threshold": 0.012,          # energy VAD, RMS in 0..1
        "start_chime": True,
    },
    "wakeword": {
        "enabled": False,
        "engine": "openwakeword",
        "model": "hey_jarvis",              # or a path to a custom "hey toony" model
        "model_dir": str(WAKEWORD_DIR),
        "threshold": 0.5,
        "cooldown_s": 2.0,
    },
    "ptt": {
        "enabled": True,
        "mode": "toggle",                   # toggle | hold
        "shortcut": "Meta+Space",           # registered as a KDE global shortcut
    },
    "tools": {
        "enabled": ["*"],                   # "*" = every registered tool
        "disabled": [],
        # What to do with each risk class: allow | ask | deny
        "policy_safe": "allow",
        "policy_sensitive": "ask",
        "policy_dangerous": "deny",
        "confirm_timeout_s": 20,
    },
    "tools.shell": {
        "enabled": False,
        "allowlist": ["ls", "cat", "df", "free", "uptime", "systemctl status"],
        "timeout_s": 15,
    },
    "tools.web": {
        "engine": "duckduckgo",
        "max_results": 5,
        "browser": "",                      # "" = xdg-open
    },
    "memory": {
        "enabled": True,
        "max_facts": 200,
    },
}

_ENV_PREFIX = "TOONY_"


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    """Turn the dotted-section DEFAULTS above into a real nested dict."""
    out: dict[str, Any] = {}
    for section, values in flat.items():
        node = out
        for part in section.split("."):
            node = node.setdefault(part, {})
        node.update(copy.deepcopy(values))
    return out


def default_config() -> dict[str, Any]:
    return _nest(DEFAULTS)


class Config:
    """A loaded configuration. Read with :meth:`get`, write with :meth:`set`."""

    def __init__(self, data: dict[str, Any] | None = None, path=None):
        self.path = path or CONFIG_FILE
        self.data = _deep_merge(default_config(), data or {})

    # ---- loading / saving -------------------------------------------------
    @classmethod
    def load(cls, path=None) -> "Config":
        path = path or CONFIG_FILE
        user: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "rb") as fh:
                user = tomllib.load(fh)
        cfg = cls(user, path=path)
        cfg._apply_env()
        return cfg

    def _apply_env(self) -> None:
        """TOONY_BRAIN__PROVIDER=claude overrides [brain] provider."""
        for env_key, raw in os.environ.items():
            if not env_key.startswith(_ENV_PREFIX):
                continue
            dotted = env_key[len(_ENV_PREFIX):].lower().replace("__", ".")
            if self.has(dotted):
                self.set(dotted, raw, save=False)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = dumps(self.data)
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)

    # ---- dotted access ----------------------------------------------------
    def get(self, dotted: str, fallback: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return fallback
            node = node[part]
        return node

    def has(self, dotted: str) -> bool:
        sentinel = object()
        return self.get(dotted, sentinel) is not sentinel

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted, {})
        return value if isinstance(value, dict) else {}

    def set(self, dotted: str, value: Any, save: bool = True) -> Any:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise KeyError(f"{dotted}: '{part}' is not a section")
        current = node.get(parts[-1])
        coerced = coerce(value, current)
        node[parts[-1]] = coerced
        if save:
            self.save()
        return coerced

    def unset(self, dotted: str, save: bool = True) -> None:
        """Restore a key to its default value."""
        default = Config().get(dotted)
        if default is None and not Config().has(dotted):
            raise KeyError(dotted)
        self.set(dotted, default, save=save)

    def flatten(self) -> dict[str, Any]:
        out: dict[str, Any] = {}

        def walk(node: dict, prefix: str) -> None:
            for key, value in node.items():
                dotted = f"{prefix}{key}"
                if isinstance(value, dict):
                    walk(value, dotted + ".")
                else:
                    out[dotted] = value

        walk(self.data, "")
        return out

    # ---- credentials ------------------------------------------------------
    def api_key(self, section: str) -> str:
        """Resolve a key: explicit value first, then the named env var."""
        sec = self.section(section)
        key = str(sec.get("api_key") or "").strip()
        if key:
            return key
        env = str(sec.get("api_key_env") or "").strip()
        return os.environ.get(env, "").strip() if env else ""


def coerce(value: Any, like: Any) -> Any:
    """Coerce a CLI string to the type of the value it replaces."""
    if not isinstance(value, str) or isinstance(like, str):
        return value
    text = value.strip()
    if isinstance(like, bool):
        if text.lower() in ("true", "yes", "on", "1"):
            return True
        if text.lower() in ("false", "no", "off", "0"):
            return False
        raise ValueError(f"expected a boolean, got {value!r}")
    if isinstance(like, int):
        return int(text)
    if isinstance(like, float):
        return float(text)
    if isinstance(like, list):
        return [p.strip() for p in text.split(",") if p.strip()]
    return value


# ---- a small TOML writer (tomllib only reads) ------------------------------

def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    text = str(value)
    escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\t", "\\t"))
    return f'"{escaped}"'


def dumps(data: dict[str, Any], _prefix: str = "") -> str:
    scalars, tables = [], []
    for key, value in data.items():
        (tables if isinstance(value, dict) else scalars).append((key, value))
    lines = [f"{k} = {_fmt(v)}" for k, v in scalars]
    for key, value in tables:
        name = f"{_prefix}{key}"
        lines.append("")
        lines.append(f"[{name}]")
        body = dumps(value, _prefix=f"{name}.")
        if body:
            lines.append(body)
    return "\n".join(line for line in lines if line is not None).strip() + "\n"
