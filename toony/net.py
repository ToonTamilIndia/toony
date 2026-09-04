"""Is there a network right now?

Every part of Toony that can fall back to something local needs the same
answer, and needs it fast: the brain router picking between a cloud model and
Ollama, the Telegram bridge deciding whether to poll, ``toony doctor``.

Asking the operating system is not enough. A laptop on a hotel wifi has a
route, a default gateway and an IP address, and still cannot reach anything.
So the check is an actual TCP connection to a host that is up, with the answer
cached for a few seconds — the router asks once per turn, and a fresh three-way
handshake per turn would be its own latency problem.
"""

from __future__ import annotations

import socket
import threading
import time

from .log import get

log = get("net")

# Three different operators, so one of them being down is not "the internet is
# down". Port 443 rather than a ping: ICMP is blocked on plenty of networks,
# and a captive portal answers TCP but not TLS to these.
PROBES = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))

# How long an answer is reused. Long enough that a burst of callers costs one
# probe, short enough that plugging in an ethernet cable is noticed.
TTL = 5.0


class Connectivity:
    """A cached, thread-safe answer to "are we online?"."""

    def __init__(self, ttl: float = TTL, probes=PROBES, timeout: float = 1.5):
        self.ttl = ttl
        self.probes = probes
        self.timeout = timeout
        self._lock = threading.Lock()
        self._answer: bool | None = None
        self._checked = 0.0
        self.changes = 0

    def _probe(self) -> bool:
        for host, port in self.probes:
            try:
                with socket.create_connection((host, port), timeout=self.timeout):
                    return True
            except OSError:
                continue
        return False

    def online(self, force: bool = False) -> bool:
        now = time.monotonic()
        with self._lock:
            fresh = self._answer is not None and now - self._checked < self.ttl
            if fresh and not force:
                return bool(self._answer)
        result = self._probe()
        with self._lock:
            if self._answer is not None and result != self._answer:
                self.changes += 1
                log.info("network went %s", "up" if result else "down")
            self._answer = result
            self._checked = time.monotonic()
        return result

    def offline(self, force: bool = False) -> bool:
        return not self.online(force=force)

    def note_failure(self) -> None:
        """A call just failed with something that smells like the network.

        Expires the cache so the next :meth:`online` really probes, instead of
        answering "yes" from a reading taken before the cable was pulled.
        """
        with self._lock:
            self._checked = 0.0

    def note_success(self) -> None:
        """Something reached the internet — no need to probe to know that."""
        with self._lock:
            self._answer = True
            self._checked = time.monotonic()


# The one everything shares, so the cache is actually shared.
NETWORK = Connectivity()


def online(force: bool = False) -> bool:
    return NETWORK.online(force=force)


def reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """Can we open a TCP connection to this specific place?

    Used for local services — Ollama on 11434 is "reachable" whether or not
    there is any internet at all.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def endpoint_reachable(base_url: str, timeout: float = 1.0) -> bool:
    """Same, for a base URL like ``http://localhost:11434/v1``."""
    from urllib.parse import urlparse

    parsed = urlparse(base_url if "//" in base_url else f"//{base_url}")
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return reachable(host, port, timeout=timeout)


def is_local(base_url: str) -> bool:
    """Whether this endpoint lives on this machine (or the LAN)."""
    from urllib.parse import urlparse

    parsed = urlparse(base_url if "//" in base_url else f"//{base_url}")
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", ""):
        return True
    return (host.startswith("192.168.") or host.startswith("10.")
            or host.endswith(".local"))
