"""The control socket.

The daemon listens on a unix socket; `toony listen`, `toony ask` and the KDE
global shortcut are all just clients sending one JSON line. This is what makes
push-to-talk work on Wayland, where an application cannot grab a global hotkey.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any, Callable

from .log import get
from .paths import socket_path

log = get("ipc")

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class ControlServer:
    def __init__(self, handler: Handler):
        self.handler = handler
        self.path = socket_path()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

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
            try:
                response = self.handler(request)
            except Exception as exc:
                log.exception("control command failed")
                response = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            _send(conn, response)


def _send(conn: socket.socket, payload: dict[str, Any]) -> None:
    try:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    except OSError:
        pass


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
