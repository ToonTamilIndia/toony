"""The window's end of the control socket.

Qt widgets must only be touched from the GUI thread, and the socket must never
be waited on from it, so everything here crosses that line exactly once: worker
threads do the blocking work and hand results back as signals.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal

from .. import ipc
from ..log import get

log = get("ui.client")


class _CallSignals(QObject):
    done = Signal(dict)


class _Call(QRunnable):
    """One request/response on a pool thread."""

    def __init__(self, command: str, payload: dict, timeout: float):
        super().__init__()
        self.command = command
        self.payload = payload
        self.timeout = timeout
        self.signals = _CallSignals()

    def run(self) -> None:
        reply = ipc.send(self.command, timeout=self.timeout, **self.payload)
        self.signals.done.emit(reply)


class EventThread(QThread):
    """Follows the daemon's event stream, reconnecting whenever it drops."""

    event = Signal(dict)
    connected = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                stream = ipc.subscribe()
                self.connected.emit(True)
                backoff = 1.0
                for message in stream:
                    if not self._running:
                        break
                    self.event.emit(message)
            except OSError as exc:
                log.debug("event stream unavailable: %s", exc)
            except Exception:
                log.exception("event stream failed")
            if not self._running:
                break
            self.connected.emit(False)
            # The daemon may simply be restarting; back off gently rather than
            # hammering a socket that is not there.
            slept = 0.0
            while self._running and slept < backoff:
                time.sleep(0.2)
                slept += 0.2
            backoff = min(backoff * 1.6, 10.0)


class DaemonClient(QObject):
    """Send commands, receive events. One of these per window."""

    event = Signal(dict)
    connected = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.online = False
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._stream = EventThread(self)
        self._stream.event.connect(self.event)
        self._stream.connected.connect(self._on_connected)

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.wait(2000)
        self._pool.waitForDone(2000)

    def _on_connected(self, online: bool) -> None:
        self.online = online
        self.connected.emit(online)

    def send(self, command: str, on_reply: Callable[[dict], None] | None = None,
             timeout: float = 30.0, **payload: Any) -> None:
        """Fire a command; ``on_reply`` runs on the GUI thread when it answers."""
        call = _Call(command, payload, timeout)
        if on_reply is not None:
            call.signals.done.connect(on_reply)
        self._pool.start(call)
