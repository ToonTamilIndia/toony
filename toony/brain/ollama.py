"""Keeping the local model loaded, and other Ollama-specific housekeeping.

The OpenAI-compatible endpoint Toony talks to cannot express any of this, so it
goes to Ollama's own API alongside.

The slow answer everybody hits is not the model thinking. It is the model not
being *there*: Ollama unloads weights five minutes after the last request, so
the first question after a coffee break spends ten to twenty seconds reading a
five-gigabyte file off disk and onto the GPU before a single token is produced.
Asking twice in a row feels fine, which is exactly why the problem survives
being investigated.

The fix is two lines of HTTP. A request to ``/api/generate`` with no prompt
loads a model and sets how long it stays; repeating that quietly in the
background keeps it resident for as long as you are actually using Toony, and
lets the GPU go back to sleep overnight.

Nothing here sets model *options*. Ollama keys a loaded runner by its load
parameters, so warming with a ``num_ctx`` the chat request does not also send
would load the model twice — the opposite of the point.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

from ..log import get
from ..net import endpoint_reachable

log = get("brain.ollama")

DEFAULT_BASE = "http://localhost:11434/v1"


def api_root(base_url: str) -> str:
    """http://host:11434/v1 -> http://host:11434."""
    return re.sub(r"/v1/?$", "", (base_url or DEFAULT_BASE).rstrip("/"))


def _post(base_url: str, path: str, payload: dict, timeout: float = 60.0):
    request = urllib.request.Request(
        f"{api_root(base_url)}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _get(base_url: str, path: str, timeout: float = 5.0):
    with urllib.request.urlopen(f"{api_root(base_url)}{path}",
                                timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def keep_loaded(model: str, base_url: str = DEFAULT_BASE,
                keep_alive: str = "30m", timeout: float = 120.0) -> bool:
    """Load ``model`` and keep it in memory. True if Ollama took the request.

    The first call is slow — it is the load. Later ones are instant and only
    push the unload timer back.
    """
    if not model:
        return False
    if not endpoint_reachable(base_url, timeout=1.0):
        return False
    try:
        _post(base_url, "/api/generate",
              {"model": model, "keep_alive": keep_alive, "prompt": "",
               "stream": False}, timeout=timeout)
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.debug("could not warm %s: %s", model, exc)
        return False


def unload(model: str, base_url: str = DEFAULT_BASE) -> bool:
    """Let go of the GPU memory now."""
    try:
        _post(base_url, "/api/generate",
              {"model": model, "keep_alive": 0, "prompt": "", "stream": False},
              timeout=15.0)
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def loaded(base_url: str = DEFAULT_BASE) -> list[dict]:
    """What Ollama currently has in memory, with sizes and expiry."""
    try:
        return list(_get(base_url, "/api/ps").get("models", []))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []


def describe_loaded(base_url: str = DEFAULT_BASE) -> str:
    entries = loaded(base_url)
    if not entries:
        return "nothing loaded — the next question pays the load time"
    parts = []
    for entry in entries:
        size = entry.get("size_vram") or entry.get("size") or 0
        where = "GPU" if entry.get("size_vram") else "RAM"
        parts.append(f"{entry.get('name', '?')} ({size / 1e9:.1f}GB in {where})")
    return ", ".join(parts)


def pull(model: str, base_url: str = DEFAULT_BASE, timeout: float = 3600.0):
    """Download a model, yielding progress lines. Used by `toony models --pull`."""
    request = urllib.request.Request(
        f"{api_root(base_url)}/api/pull",
        data=json.dumps({"model": model, "stream": True}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


class WarmKeeper:
    """Pushes the unload timer back for as long as Toony is being used.

    Not a permanent pin: if nobody has said anything for
    ``keep_warm_minutes``, the heartbeat stops and Ollama frees the card in its
    own time. So an afternoon of questions is fast throughout, and a laptop
    left alone overnight does not sit on five gigabytes of VRAM.
    """

    def __init__(self, config, model_of=None):
        self.config = config
        # A callable, because the router can change which model is in use
        # halfway through the day.
        self._model_of = model_of
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._wake = threading.Event()
        self.last_use = time.monotonic()
        self.warmed = 0

    # ---- settings ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("brain.ollama.keep_warm", True))

    @property
    def base_url(self) -> str:
        return str(self.config.get("brain.ollama.base_url", DEFAULT_BASE))

    @property
    def keep_alive(self) -> str:
        return str(self.config.get("brain.ollama.keep_alive", "30m"))

    @property
    def interval(self) -> float:
        return max(30.0, float(self.config.get("brain.ollama.warm_interval_s", 240)))

    @property
    def idle_limit(self) -> float:
        return max(0.0, float(self.config.get("brain.ollama.keep_warm_minutes",
                                              90))) * 60

    def model(self) -> str:
        if self._model_of is not None:
            try:
                return self._model_of() or ""
            except Exception:
                return ""
        return str(self.config.get("brain.ollama.model", ""))

    # ---- lifecycle --------------------------------------------------------
    def touch(self) -> None:
        """Somebody used Toony. Restarts the idle countdown."""
        self.last_use = time.monotonic()
        self._wake.set()

    def warm_now(self, blocking: bool = False) -> bool:
        """Load the model right now. The first call can take a while."""
        model = self.model()
        if not model:
            return False
        if blocking:
            started = time.monotonic()
            ok = keep_loaded(model, self.base_url, self.keep_alive)
            if ok:
                self.warmed += 1
                log.info("kept %s loaded (%.1fs, unloads after %s of quiet)",
                         model, time.monotonic() - started, self.keep_alive)
            return ok
        threading.Thread(target=self.warm_now, args=(True,),
                         name="toony-ollama-warm", daemon=True).start()
        return True

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="toony-ollama",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        self.warm_now(blocking=True)
        while self._running.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if not self._running.is_set():
                break
            if not self.enabled:
                continue
            idle = time.monotonic() - self.last_use
            if self.idle_limit and idle > self.idle_limit:
                continue        # let Ollama have the memory back
            self.warm_now(blocking=True)
