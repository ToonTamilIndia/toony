"""Starting the GUI: one window, one tray icon, one instance.

The window is a client of the daemon, not a replacement for it. If the daemon
is not running the tray still comes up and says so, because a tray icon that
disappears when the service hiccups is worse than one that reports the problem.
"""

from __future__ import annotations

import os
import socket
import sys
import threading

from ..config import Config
from ..log import get, setup
from ..paths import ensure_dirs, runtime_dir

log = get("ui")


class SingleInstance:
    """A lock plus a doorbell.

    The lock stops a second window opening. The doorbell means the second
    launch is not wasted: it tells the window that is already running to show
    itself, which is exactly what a user pressing the launcher twice wants.
    """

    def __init__(self, on_show):
        self.on_show = on_show
        self.path = runtime_dir() / "ui.sock"
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def claim(self) -> bool:
        """True if we are the first instance; False after ringing the doorbell."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if self._ring():
                return False
            self.path.unlink(missing_ok=True)   # stale, from a crashed window
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self._sock.listen(4)
            self._sock.settimeout(0.5)
        except OSError as exc:
            log.warning("could not claim the window lock: %s", exc)
            return True
        self._running = True
        self._thread = threading.Thread(target=self._serve, name="toony-ui-lock",
                                        daemon=True)
        self._thread.start()
        return True

    def _ring(self) -> bool:
        """Tell the running window to show itself, and lend it our token.

        This process was launched by the desktop, so the compositor handed it
        an xdg-activation token. The window already running has none — under
        Wayland it cannot focus itself — so passing ours across is the only
        way a second click on the launcher actually brings it forward.
        """
        token = (os.environ.get("XDG_ACTIVATION_TOKEN", "")
                 or os.environ.get("DESKTOP_STARTUP_ID", ""))
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(str(self.path))
                sock.sendall(f"show {token}\n".encode("utf-8", "replace"))
                return True
        except OSError:
            return False

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            token = ""
            with conn:
                try:
                    conn.settimeout(1.0)
                    raw = conn.recv(4096).decode("utf-8", "replace").strip()
                    parts = raw.split(" ", 1)
                    token = parts[1].strip() if len(parts) > 1 else ""
                except OSError:
                    pass
            self.on_show(token)

    def release(self) -> None:
        self._running = False
        if self._sock:
            self._sock.close()
        self.path.unlink(missing_ok=True)


def _notify_send(message: str) -> bool:
    """A desktop notification without Qt, for when there is no tray icon."""
    import shutil
    import subprocess

    if not shutil.which("notify-send"):
        return False
    try:
        subprocess.Popen(["notify-send", "--app-name=Toony", "--icon=toony",
                          "Toony", message], start_new_session=True)
        return True
    except OSError:
        return False


def run(start_hidden: bool | None = None) -> int:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    except ImportError:
        print("The Toony window needs PySide6.\n"
              "  pip install 'toony[gui]'      (or: sudo dnf install python3-pyside6)",
              file=sys.stderr)
        return 1

    from . import avatar
    from .client import DaemonClient
    from .settings import SettingsDialog
    from .window import ToonyWindow

    ensure_dirs()
    config = Config.load()
    setup(str(config.get("general.log_level", "info")), to_file=True)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Toony")
    app.setApplicationDisplayName(str(config.get("general.name", "Toony")))
    app.setDesktopFileName("toony")
    app.setQuitOnLastWindowClosed(False)     # the tray keeps it alive

    accent = str(config.get("ui.accent", "#7c5cff"))
    url = str(config.get("ui.avatar_url", ""))
    icon = avatar.window_icon(url, accent)
    app.setWindowIcon(icon)

    client = DaemonClient()
    window = ToonyWindow(config, client)
    window.setWindowIcon(icon)

    # A second `toony gui` is launched by the desktop, so it is handed an
    # activation token; the running window spends it to come forward.
    def show_with(token: str = "") -> None:
        window.pop_up(token)

    # ---- settings ---------------------------------------------------------
    def open_settings() -> None:
        dialog = SettingsDialog(config, window, on_preview=window.set_opacity)
        dialog.applied.connect(apply_changes)
        if dialog.exec() == 0:
            window.set_opacity(float(config.get("ui.opacity", 0.97)))

    def apply_changes(changes: dict) -> None:
        if not changes:
            return
        log.info("saving %d setting(s): %s", len(changes), ", ".join(sorted(changes)))
        if client.online:
            # Let the daemon write and reload, so it never runs on stale settings.
            client.send("config", lambda reply: reloaded(reply, changes),
                        timeout=60, action="set", values=changes)
        else:
            for key, value in changes.items():
                config.set(key, value, save=False)
            config.save()
            window.apply_style()

    def reloaded(reply: dict, changes: dict) -> None:
        if not reply.get("ok"):
            log.warning("the daemon refused the change: %s", reply.get("error"))
            notify(f"Toony could not apply that: {reply.get('error', 'unknown')}")
            return
        config.data = Config.load().data
        window.config = config
        window.apply_style()
        window.refresh()

    window.on_settings = open_settings

    # ---- tray -------------------------------------------------------------
    tray = None
    if config.get("ui.tray", True) and QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(icon, app)
        tray.setToolTip("Toony")
        menu = QMenu()
        menu.addAction("Talk to Toony", window.start_listening)
        menu.addAction("Stop talking", window.interrupt)
        menu.addAction("Show / hide window", window.toggle_visible)
        menu.addSeparator()
        menu.addAction("New conversation", window.new_conversation)
        menu.addAction("Settings…", open_settings)
        menu.addSeparator()
        menu.addAction("Stop the assistant",
                       lambda: client.send("quit", timeout=10))
        menu.addAction("Quit the window", app.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: window.toggle_visible()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        window.setStyleSheet(window.styleSheet())   # menus inherit the sheet
        menu.setStyleSheet(window.styleSheet())

    def notify(message: str) -> None:
        """The one way to reach the user that Wayland never refuses."""
        if tray is not None:
            tray.showMessage("Toony", message, icon, 8000)
            return
        if _notify_send(message):
            return
        log.info(message)

    window.on_attention = notify

    def on_connected(online: bool) -> None:
        if tray is not None:
            tray.setToolTip("Toony — ready" if online else "Toony — not running")

    client.connected.connect(on_connected)

    # ---- single instance --------------------------------------------------
    lock = SingleInstance(
        on_show=lambda token: QTimer.singleShot(0, lambda: show_with(token)))
    if not lock.claim():
        print("Toony's window is already open.")
        return 0
    app.aboutToQuit.connect(lock.release)
    app.aboutToQuit.connect(client.stop)
    app.aboutToQuit.connect(window.remember_size)

    client.start()
    hidden = (config.get("ui.start_minimised", True)
              if start_hidden is None else start_hidden)
    if not hidden or tray is None:
        window.show()
        window.composer.setFocus()
    QTimer.singleShot(300, window.refresh)
    return app.exec()
