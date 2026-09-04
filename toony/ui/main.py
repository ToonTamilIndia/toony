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


_TRAY_TOOLTIP = {
    "idle": "ready", "starting": "starting up", "listening": "listening",
    "thinking": "thinking", "speaking": "speaking", "offline": "not running",
}

_MENU_STATUS = {
    "idle": "Ready", "starting": "Starting up…", "listening": "Listening…",
    "thinking": "Thinking…", "speaking": "Speaking…",
    "offline": "Not running — start it with: toony start",
}


def _menu_icon(name: str, config):
    """Menu artwork in the palette's own text colour, or nothing."""
    from . import icons, theme

    colours = theme.palette(str(config.get("ui.theme", "auto")),
                            str(config.get("ui.accent", "#7c5cff")))
    return icons.icon(name, colours["text"])


def _act(menu, label: str, handler, config=None, shape: str = ""):
    action = menu.addAction(label)
    action.triggered.connect(lambda _checked=False: handler())
    if shape and config is not None:
        drawn = _menu_icon(shape, config)
        if drawn is not None:
            action.setIcon(drawn)
    return action


def _build_menu(app, window, client, config, orb, open_settings, state):
    """The tray menu, rebuilt every time it opens.

    It used to be filled once at start-up, which meant every tick box in it
    showed whatever the setting had been when Toony launched — turn the wake
    word off in Settings and the tray still claimed it was on. Rebuilding on
    ``aboutToShow`` costs nothing and can never be stale.
    """
    from PySide6.QtWidgets import QMenu

    menu = QMenu()
    menu.aboutToShow.connect(
        lambda: _fill_menu(menu, app, window, client, config, orb,
                           open_settings, state()))
    _fill_menu(menu, app, window, client, config, orb, open_settings, state())
    return menu


def _fill_menu(menu, app, window, client, config, orb, open_settings,
               state: str = "idle") -> None:
    """The menu behind the tray icon and the orb. Both show the same thing.

    Everything here is something you would otherwise open a terminal for.
    """
    menu.clear()
    name = str(config.get("general.name", "Toony"))
    heading = menu.addAction(f"{name} — {_MENU_STATUS.get(state, state)}")
    heading.setEnabled(False)
    menu.addSeparator()

    busy = state in ("listening", "thinking", "speaking")
    if busy:
        _act(menu, "Stop talking", window.interrupt, config, "stop")
    else:
        _act(menu, "Talk to Toony", window.start_listening, config, "mic")
    _act(menu, "Hide the window" if window.isVisible() else "Open the window",
         window.toggle_visible, config, "menu")
    _act(menu, "New conversation", window.new_conversation, config, "new")

    recent = menu.addMenu("Recent conversations")
    recent.aboutToShow.connect(lambda: _fill_recent(recent, client, window))

    menu.addSeparator()
    pin = menu.addAction("Pin to the desktop")
    pin.setCheckable(True)
    pin.setChecked(bool(getattr(window, "pinned", False)))
    pin.setToolTip("Keep Toony in front, on every virtual desktop.")
    pin.toggled.connect(window.set_pinned)

    routines = menu.addMenu("Routines")
    routines.aboutToShow.connect(lambda: _fill_routines(routines, client, config))

    quick = menu.addMenu("Quick settings")
    _add_toggle(quick, "Wake word", config, client, "wakeword.enabled")
    _add_toggle(quick, "Run routines", config, client, "automation.enabled")
    _add_toggle(quick, "Stop when I talk over it", config, client,
                "audio.barge_in")
    _add_toggle(quick, "Speak replies", config, client, "tts.stream")
    if orb is not None:
        _act(quick, "Show the orb" if orb.isVisible() else "Hide the orb",
             orb.hide if orb.isVisible() else orb.show)

    personality = quick.addMenu("Personality")
    current = str(config.get("general.personality"))
    for style in ("plain", "friendly", "spicy"):
        action = personality.addAction(style.capitalize())
        action.setCheckable(True)
        action.setChecked(current == style)
        action.triggered.connect(
            lambda _checked, s=style: _set(client, config,
                                           "general.personality", s))

    _act(menu, "Settings…", open_settings, config, "settings")
    menu.addSeparator()
    _act(menu, "Restart the assistant", lambda: client.send("reload", timeout=60))
    _act(menu, "Quit", app.quit, config, "close")


def _add_toggle(menu, label: str, config, client, key: str) -> None:
    action = menu.addAction(label)
    action.setCheckable(True)
    action.setChecked(bool(config.get(key)))
    action.toggled.connect(lambda on: _set(client, config, key, on))


def _set(client, config, key: str, value) -> None:
    config.set(key, value, save=False)
    client.send("config", timeout=60, action="set", values={key: value})


def _fill_routines(menu, client, config) -> None:
    """The things Toony does on its own, and whether they are due.

    Filled when the submenu opens rather than when the tray is built: "in 42
    minutes" is only true at the moment it is read.
    """
    menu.clear()
    menu.addAction("Loading…").setEnabled(False)

    def arrived(reply: dict) -> None:
        menu.clear()
        entries = ((reply.get("routines") or {}).get("routines", [])
                   if reply.get("ok") else [])
        if not entries:
            hint = menu.addAction("No routines yet")
            hint.setEnabled(False)
            tip = menu.addAction('Add one:  toony routine add')
            tip.setEnabled(False)
            return
        for entry in entries:
            due = entry.get("in_s")
            when = entry["when"] if due is None else f"{entry['when']} · in {_soon(due)}"
            action = menu.addAction(f"{entry['name']}   ({when})")
            action.setCheckable(True)
            action.setChecked(bool(entry.get("enabled", True)))
            action.setToolTip(entry.get("error") or "")
            action.triggered.connect(
                lambda checked, name=entry["name"]: _toggle_routine(
                    client, config, name, checked))

    client.send("status", arrived, timeout=5)


def _toggle_routine(client, config, name: str, enabled: bool) -> None:
    routines = list(config.get("automation.routines", []) or [])
    for entry in routines:
        if isinstance(entry, dict) and entry.get("name") == name:
            entry["enabled"] = enabled
    _set(client, config, "automation.routines", routines)


def _soon(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600}h"


def _fill_recent(menu, client, window) -> None:
    """Filled when the submenu opens, so it is never stale."""
    menu.clear()
    pending = menu.addAction("Loading…")
    pending.setEnabled(False)

    def arrived(reply: dict) -> None:
        menu.clear()
        rows = reply.get("conversations", []) if reply.get("ok") else []
        if not rows:
            empty = menu.addAction("Nothing yet")
            empty.setEnabled(False)
            return
        for row in rows[:8]:
            action = menu.addAction(row.get("title", "Conversation"))
            action.triggered.connect(
                lambda _checked, i=row.get("id"): _open(window, i))

    client.send("conversations", arrived, timeout=10, limit=8)


def _open(window, conversation_id: str) -> None:
    window.client.send("conversation", window._on_opened, timeout=15,
                       action="open", id=conversation_id)
    window.toggle_visible()


def _tray_clicked(reason, window) -> None:
    """Left click opens the window; middle click starts a turn straight away.

    Middle click is the one gesture nothing else on a Plasma tray uses, which
    makes it the right home for "just listen to me" — no menu, no window.
    """
    from PySide6.QtWidgets import QSystemTrayIcon

    if reason == QSystemTrayIcon.ActivationReason.Trigger:
        window.toggle_visible()
    elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
        window.start_listening()


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
    from .orb import build as build_orb
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
    name = str(config.get("general.name", "Toony"))
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
        if orb is not None:
            orb.config = config
            orb.reload_avatar()
        # The tray menu carries its own copy of the sheet, so a new theme or
        # accent has to be handed to it as well or it keeps the old colours.
        if tray is not None and tray.contextMenu() is not None:
            tray.contextMenu().setStyleSheet(window.styleSheet())
        window.refresh()

    window.on_settings = open_settings

    # ---- the orb ----------------------------------------------------------
    orb = build_orb(config)
    if orb is not None:
        orb.clicked.connect(window.start_listening)
        orb.opened.connect(window.toggle_visible)
        # Right-clicking the orb offers exactly what the tray does.
        orb.build_menu = lambda menu: _fill_menu(menu, app, window, client,
                                                 config, orb, open_settings,
                                                 current["state"])

    # The one place the assistant's state lives on this side of the socket.
    # The tray menu reads it when it opens, so it can say what Toony is doing
    # and offer "Stop talking" rather than "Talk to Toony".
    current = {"state": "starting"}

    def set_state(state: str) -> None:
        """One state, three places: the window, the orb and the tray icon."""
        current["state"] = state
        if orb is not None:
            orb.set_state(state)
        if tray is not None:
            tray.setIcon(avatar.state_icon(state, url, accent, name))
            tray.setToolTip(f"{name} — {_TRAY_TOOLTIP.get(state, state)}")

    def on_event(event: dict) -> None:
        kind = str(event.get("event", ""))
        if kind == "state":
            set_state(str(event.get("state", "idle")))
        elif kind == "confirm" and orb is not None:
            orb.set_state("thinking")
        elif kind == "brain":
            # The model answering changed underneath you. Worth a notification:
            # it is the difference between "the cloud one" and "the local one",
            # and nothing else on screen would say so.
            message = str(event.get("message", ""))
            if message:
                notify(message)
        elif kind == "routine":
            log.info("routine %s: %s", event.get("name"), event.get("prompt"))

    client.event.connect(on_event)

    # ---- tray ---------------------------------------------------------------
    tray = None
    if config.get("ui.tray", True) and QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(avatar.state_icon("idle", url, accent, name), app)
        tray.setToolTip(f"{name} — starting")
        menu = _build_menu(app, window, client, config, orb, open_settings,
                           lambda: current["state"])
        menu.setStyleSheet(window.styleSheet())
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: _tray_clicked(reason, window))
        tray.show()

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
        if not online:
            set_state("offline")

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
    if orb is not None:
        orb.show_at_corner()
    hidden = (config.get("ui.start_minimised", True)
              if start_hidden is None else start_hidden)
    if not hidden or (tray is None and orb is None):
        window.show()
        window.composer.setFocus()
    QTimer.singleShot(300, window.refresh)
    return app.exec()
