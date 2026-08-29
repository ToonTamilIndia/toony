"""The control socket.

The daemon listens on a unix socket; `toony listen`, `toony ask` and the KDE
global shortcut are all just clients sending one JSON line. This is what makes
push-to-talk work on Wayland, where an application cannot grab a global hotkey.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
from typing import Any, Callable, Iterator

from .log import get
from .paths import socket_path

log = get("ipc")

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class ControlServer:
    """One JSON line in, one JSON line out — except for ``subscribe``.

    A subscriber keeps its connection open and receives every event the daemon
    publishes, which is how the GUI follows a voice turn it did not start.
    """

    def __init__(self, handler: Handler):
        self.handler = handler
        self.path = socket_path()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    # ---- events -----------------------------------------------------------
    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def broadcast(self, event: dict[str, Any]) -> None:
        """Push one event to every open subscriber. Never blocks the daemon."""
        with self._lock:
            targets = list(self._subscribers)
        for target in targets:
            try:
                target.put_nowait(event)
            except queue.Full:
                log.debug("dropping an event for a subscriber that fell behind")

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # A stale socket from a crashed daemon, or a live one we must not steal.
            if _ping(self.path):
                raise RuntimeError(
                    f"Toony is already running (socket {self.path}). "
                    "Stop it with: systemctl --user stop toony")
            self.path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        os.chmod(self.path, 0o600)  # only this user may drive the assistant
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._running.set()
        self._thread = threading.Thread(target=self._serve, name="toony-ipc",
                                        daemon=True)
        self._thread.start()
        log.info("control socket at %s", self.path)

    def stop(self) -> None:
        self._running.clear()
        self.broadcast({"event": "closing"})
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._sock:
            self._sock.close()
        try:
            self.path.unlink()
        except OSError:
            pass

    def _serve(self) -> None:
        while self._running.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(120)
            try:
                raw = conn.makefile("rb").readline()
                if not raw:
                    return
                request = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                _send(conn, {"ok": False, "error": f"bad request: {exc}"})
                return
            if request.get("command") == "subscribe":
                self._stream(conn, request)
                return
            try:
                response = self.handler(request)
            except Exception as exc:
                log.exception("control command failed")
                response = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            _send(conn, response)

    def _stream(self, conn: socket.socket, request: dict[str, Any]) -> None:
        """Hold a connection open and write events to it until it goes away."""
        events: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(events)
        conn.settimeout(None)
        log.info("event subscriber attached (%d total)", self.subscribers)
        try:
            _send(conn, {"ok": True, "event": "subscribed"})
            while self._running.is_set():
                try:
                    event = events.get(timeout=5.0)
                except queue.Empty:
                    # A keepalive is how a dead peer is noticed: sendall fails.
                    if not _send(conn, {"event": "keepalive"}):
                        break
                    continue
                if not _send(conn, event):
                    break
        finally:
            with self._lock:
                self._subscribers.discard(events)
            log.info("event subscriber gone (%d left)", self.subscribers)


def _send(conn: socket.socket, payload: dict[str, Any]) -> bool:
    try:
        conn.sendall((json.dumps(payload, default=str) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def _ping(path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(path))
            sock.sendall(b'{"command": "ping"}\n')
            return bool(sock.recv(64))
    except OSError:
        return False


def send(command: str, timeout: float = 120.0, **payload) -> dict[str, Any]:
    """Send one command to a running daemon and return its reply."""
    path = socket_path()
    request = {"command": command, **payload}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            raw = sock.makefile("rb").readline()
    except FileNotFoundError:
        return {"ok": False, "error": "Toony is not running. "
                                      "Start it with: systemctl --user start toony"}
    except ConnectionRefusedError:
        return {"ok": False, "error": "Toony's socket is stale. "
                                      "Restart it: systemctl --user restart toony"}
    except socket.timeout:
        return {"ok": False, "error": "Toony did not answer in time."}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if not raw:
        return {"ok": False, "error": "Toony closed the connection."}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"unreadable reply: {exc}"}


def is_running() -> bool:
    return socket_path().exists() and _ping(socket_path())


def subscribe(timeout: float = 5.0) -> Iterator[dict[str, Any]]:
    """Yield daemon events until the connection drops.

    Raises :class:`OSError` if the daemon is not there, so a caller can retry.
    """
    path = socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(path))
    sock.sendall(b'{"command": "subscribe"}\n')
    sock.settimeout(None)
    try:
        stream = sock.makefile("rb")
        for raw in stream:
            if not raw.strip():
                continue
            try:
                event = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if event.get("event") == "keepalive":
                continue
            yield event
    finally:
        sock.close()
