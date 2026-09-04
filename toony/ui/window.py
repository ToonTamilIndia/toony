"""The Toony window: conversations on the left, the exchange on the right.

Everything shown here comes from the daemon over the control socket, so the
window is a view rather than a second brain — close it and the assistant keeps
working, open it mid-sentence and it catches up.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextOption
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QScrollArea,
                               QSizeGrip, QSizePolicy, QTextEdit, QVBoxLayout,
                               QWidget)

from ..log import get
from . import avatar, icons, theme


def _wayland() -> bool:
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    return bool(app and app.platformName().startswith("wayland"))

log = get("ui.window")

_PIN_TIP = {
    True: "Pinned to the desktop — click to unpin  (Ctrl+P)",
    False: "Pin to the desktop: keep Toony in front, on every "
           "virtual desktop  (Ctrl+P)",
}

# Only ever seen if the icon cache cannot be written: a named button beats a
# blank one.
_FALLBACK_TEXT = {
    "menu": "≡", "new": "+", "settings": "…", "minimise": "–", "close": "x",
    "pin": "P", "pin_off": "P", "mic": "Talk", "stop": "Stop", "send": "Send",
}

_STATUS = {
    "idle": ("Ready", "muted"),
    "starting": ("Starting…", "muted"),
    "listening": ("Listening…", "accent"),
    "thinking": ("Thinking…", "accent"),
    "speaking": ("Speaking…", "good"),
    "offline": ("Not running", "danger"),
}


class Header(QWidget):
    """The title bar. Dragging it moves the window.

    On Wayland an application may not place its own window: `move()` does
    nothing and the compositor ignores it. `startSystemMove()` asks the
    compositor to do the dragging instead, which is the only thing that works
    there — and works on X11 too, so there is no branch.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("header")
        self._press: QPoint | None = None

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        window = self.window()
        handle = window.windowHandle()
        if handle is not None and handle.startSystemMove():
            return
        # X11 without the protocol, or an odd compositor: move it ourselves.
        self._press = event.globalPosition().toPoint() - window.pos()

    def mouseMoveEvent(self, event) -> None:
        if self._press is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._press)

    def mouseReleaseEvent(self, event) -> None:
        self._press = None

    def mouseDoubleClickEvent(self, event) -> None:
        window = self.window()
        window.showNormal() if window.isMaximized() else window.showMaximized()


class Bubble(QLabel):
    """One line of the conversation."""

    def __init__(self, text: str, kind: str = "toony"):
        super().__init__(text)
        self.setObjectName({"user": "bubbleUser", "tool": "bubbleTool"}
                           .get(kind, "bubbleToony"))
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)


class Composer(QTextEdit):
    """A text box that grows with the message and sends on Enter."""

    submitted = Signal(str)
    #: True while there is something worth sending. The Send button follows it.
    has_text = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setPlaceholderText("Ask Toony anything…   (Enter to send)")
        self.setAcceptRichText(False)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFixedHeight(42)
        self.textChanged.connect(self._grow)
        self.textChanged.connect(
            lambda: self.has_text.emit(bool(self.toPlainText().strip())))

    def _grow(self) -> None:
        height = int(self.document().size().height()) + 18
        self.setFixedHeight(max(42, min(140, height)))

    def submit(self) -> None:
        """Send what is typed, from Enter or from the Send button alike."""
        text = self.toPlainText().strip()
        if text:
            self.clear()
            self.submitted.emit(text)

    def keyPressEvent(self, event) -> None:
        enter = event.key() in (Qt.Key_Return, Qt.Key_Enter)
        if enter and not (event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier)):
            self.submit()
            return
        super().keyPressEvent(event)


class ToonyWindow(QWidget):
    """The main window. It owns no state the daemon does not also have."""

    def __init__(self, config, client, on_settings=None, on_quit=None,
                 on_attention=None):
        super().__init__()
        self.config = config
        self.client = client
        self.on_settings = on_settings
        self.on_quit = on_quit
        # Called when the window could not raise itself and something still
        # needs the user's eyes. Wired to a desktop notification.
        self.on_attention = on_attention
        self.current_conversation = ""
        self.pending_confirm = ""
        self.busy = False
        # ui.always_on_top is the old name for the same idea; honour it so an
        # existing config keeps working.
        self.pinned = bool(config.get("ui.pinned", False)
                           or config.get("ui.always_on_top", False))
        self._thinking: Bubble | None = None
        self._live: Bubble | None = None

        self.setWindowTitle(str(config.get("general.name", "Toony")))
        self.frameless = bool(config.get("ui.frameless", True))
        if self.frameless:
            self.setWindowFlag(Qt.FramelessWindowHint, True)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(380, 420)
        self.resize(int(config.get("ui.width", 460)),
                    int(config.get("ui.height", 640)))

        self._build()
        self.apply_style()
        self._wire()
        self.pin_button.setChecked(self.pinned)
        self._show_pin_state()

    # ---- construction -----------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.root = QWidget(objectName="root")
        outer.addWidget(self.root)
        layout = QVBoxLayout(self.root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._header())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._sidebar())
        body.addLayout(self._chat_column(), 1)
        layout.addLayout(body, 1)

    def _header(self) -> QWidget:
        header = Header()
        header.setFixedHeight(58)
        row = QHBoxLayout(header)
        row.setContentsMargins(12, 8, 8, 8)
        row.setSpacing(10)

        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(36, 36)
        row.addWidget(self.avatar_label)

        names = QVBoxLayout()
        names.setSpacing(0)
        self.title_label = QLabel(str(self.config.get("general.name", "Toony")),
                                  objectName="title")
        self.status_label = QLabel("Connecting…", objectName="subtitle")
        names.addWidget(self.title_label)
        names.addWidget(self.status_label)
        row.addLayout(names)
        row.addStretch(1)

        self.sidebar_button = self._icon_button("menu",
                                                "Show or hide conversations")
        self.new_button = self._icon_button("new", "New conversation")
        self.pin_button = self._icon_button("pin_off", _PIN_TIP[False],
                                            checkable=True)
        self.settings_button = self._icon_button("settings", "Settings")
        self.hide_button = self._icon_button("minimise", "Hide to the tray")
        self.close_button = self._icon_button("close", "Hide to the tray")
        for button in (self.sidebar_button, self.new_button, self.pin_button,
                       self.settings_button, self.hide_button, self.close_button):
            row.addWidget(button)
        return header

    def _icon_button(self, shape: str, tip: str,
                     checkable: bool = False) -> QPushButton:
        """A header button. The artwork is SVG, not an emoji.

        An emoji glyph is painted by the font in the font's own colours, so it
        ignores ``color:`` and disappears the moment the button is hovered on
        to the accent. A drawn shape takes the colour it is given.
        """
        button = QPushButton(objectName="icon")
        button.setToolTip(tip)
        button.setCursor(Qt.PointingHandCursor)
        button.setCheckable(checkable)
        button.setFixedSize(30, 30)
        button.setIconSize(QSize(17, 17))
        button._shape = shape
        return button

    def _sidebar(self) -> QWidget:
        self.conversation_list = QListWidget()
        self.conversation_list.setFixedWidth(168)
        self.conversation_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.conversation_list.setVisible(False)
        self.conversation_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        return self.conversation_list

    def _chat_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.messages_host = QWidget()
        self.messages = QVBoxLayout(self.messages_host)
        self.messages.setContentsMargins(14, 12, 14, 12)
        self.messages.setSpacing(8)
        self.messages.addStretch(1)
        self.scroll.setWidget(self.messages_host)
        column.addWidget(self.scroll, 1)

        column.addWidget(self._permission_bar())

        footer = QHBoxLayout()
        footer.setContentsMargins(12, 6, 12, 12)
        footer.setSpacing(8)
        self.composer = Composer()
        self.mic_button = self._round_button("mic",
                                             "Talk to Toony "
                                             "(or press the global shortcut)")
        self.send_button = self._round_button("send", "Send  (Enter)")
        self.send_button.setEnabled(False)
        footer.addWidget(self.composer, 1)
        footer.addWidget(self.mic_button, 0, Qt.AlignBottom)
        footer.addWidget(self.send_button, 0, Qt.AlignBottom)

        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 4, 4)
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)

        column.addLayout(footer)
        column.addLayout(grip_row)
        return column

    def _round_button(self, shape: str, tip: str) -> QPushButton:
        button = QPushButton(objectName=shape)
        button.setToolTip(tip)
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedSize(40, 40)
        button.setIconSize(QSize(20, 20))
        button._shape = shape
        return button

    def _permission_bar(self) -> QWidget:
        self.permission = QFrame(objectName="permission")
        self.permission.setVisible(False)
        row = QHBoxLayout(self.permission)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(8)
        self.permission_label = QLabel()
        self.permission_label.setWordWrap(True)
        self.allow_button = QPushButton("Allow", objectName="primary")
        self.deny_button = QPushButton("Deny", objectName="danger")
        row.addWidget(self.permission_label, 1)
        row.addWidget(self.deny_button)
        row.addWidget(self.allow_button)

        wrapper = QWidget()
        wrap = QVBoxLayout(wrapper)
        wrap.setContentsMargins(12, 0, 12, 0)
        wrap.addWidget(self.permission)
        return wrapper

    # ---- signals ----------------------------------------------------------
    def _wire(self) -> None:
        self.client.event.connect(self.on_event)
        self.client.connected.connect(self.on_connected)

        self.composer.submitted.connect(self.send_text)
        self.composer.has_text.connect(self.send_button.setEnabled)
        self.send_button.clicked.connect(self.composer.submit)
        self.mic_button.clicked.connect(self.start_listening)
        self.new_button.clicked.connect(self.new_conversation)
        self.pin_button.toggled.connect(self.set_pinned)
        self.sidebar_button.clicked.connect(self.toggle_sidebar)
        self.settings_button.clicked.connect(
            lambda: self.on_settings and self.on_settings())
        self.hide_button.clicked.connect(self.hide)
        self.close_button.clicked.connect(self.hide)
        self.allow_button.clicked.connect(lambda: self.answer_permission(True))
        self.deny_button.clicked.connect(lambda: self.answer_permission(False))
        self.conversation_list.itemActivated.connect(self._open_selected)
        self.conversation_list.itemClicked.connect(self._open_selected)
        self.conversation_list.customContextMenuRequested.connect(self._list_menu)

        QShortcut(QKeySequence("Ctrl+N"), self, self.new_conversation)
        QShortcut(QKeySequence("Ctrl+L"), self, self.start_listening)
        QShortcut(QKeySequence("Escape"), self, self.escape)
        QShortcut(QKeySequence("Ctrl+."), self, self.interrupt)
        QShortcut(QKeySequence("Ctrl+P"), self,
                  lambda: self.pin_button.toggle())
        QShortcut(QKeySequence("Ctrl+Comma"), self,
                  lambda: self.on_settings and self.on_settings())

    # ---- appearance -------------------------------------------------------
    def apply_style(self) -> None:
        accent = str(self.config.get("ui.accent", "#7c5cff"))
        self.set_opacity(self._opacity(), accent=accent)
        self._apply_icons(accent)
        self.avatar_label.setPixmap(avatar.circular_pixmap(
            36, str(self.config.get("ui.avatar_url", "")), accent,
            str(self.config.get("general.name", "T"))))

    def _apply_icons(self, accent: str) -> None:
        """Repaint the button artwork for the palette in force.

        An SVG is baked at one colour, so switching theme or accent means new
        files, not a restyle. They are cached, so this is cheap.
        """
        colours = theme.palette(str(self.config.get("ui.theme", "auto")), accent)
        for button in (self.sidebar_button, self.new_button, self.pin_button,
                       self.settings_button, self.hide_button, self.close_button):
            self._set_icon(button, button._shape, colours["text"])
        self._set_icon(self.mic_button,
                       "stop" if self.busy else "mic", colours["on_accent"])
        self._set_icon(self.send_button, "send", colours["on_accent"])

    @staticmethod
    def _set_icon(button: QPushButton, shape: str, colour: str) -> None:
        """Give a button its picture, or its name if the artwork is missing.

        A button with neither an icon nor a label is an unlabelled blank, which
        is worse than a word — so the text fallback is not decorative.
        """
        button._shape = shape
        drawn = icons.icon(shape, colour)
        if drawn is None:
            button.setText(_FALLBACK_TEXT.get(shape, ""))
            return
        button.setText("")
        button.setIcon(drawn)

    def _opacity(self) -> float:
        try:
            value = float(self.config.get("ui.opacity", 0.97))
        except (TypeError, ValueError):
            return 0.97
        return min(1.0, max(0.35, value))     # never let it become invisible

    def set_opacity(self, value: float, accent: str | None = None) -> None:
        """Apply translucency the way this platform actually supports it."""
        value = min(1.0, max(0.35, value))
        accent = accent or str(self.config.get("ui.accent", "#7c5cff"))
        painted = self.frameless and _wayland()
        self.setStyleSheet(theme.stylesheet(
            str(self.config.get("ui.theme", "auto")), accent,
            int(self.config.get("ui.font_size", 14)),
            opacity=value if painted else 1.0))
        # A no-op under Wayland, which is why the colours carry it there.
        self.setWindowOpacity(1.0 if painted else value)

    # ---- daemon events ----------------------------------------------------
    def on_connected(self, online: bool) -> None:
        if online:
            self.refresh()
        else:
            self._set_status("offline")

    def on_event(self, event: dict) -> None:
        kind = str(event.get("event", ""))
        if kind == "state":
            self._set_status(str(event.get("state", "idle")))
        elif kind == "heard":
            self._clear_thinking()
            self._live = None
            self.add_bubble(str(event.get("text", "")), "user")
            self._show_thinking()
        elif kind == "reply_chunk":
            self._append_live(str(event.get("text", "")))
        elif kind == "reply":
            self._finish_live(str(event.get("text", "")))
            if event.get("conversation") != self.current_conversation:
                self.current_conversation = str(event.get("conversation", ""))
            self.load_conversations()
        elif kind == "interrupted":
            self._clear_thinking()
            self._set_status("idle")
        elif kind == "tool":
            self.add_bubble(_tool_line(event), "tool")
        elif kind == "confirm":
            self.show_permission(str(event.get("id", "")),
                                 str(event.get("question", "")))
        elif kind == "conversation":
            self.current_conversation = str(event.get("id", ""))
            if event.get("action") in ("new", "open", "delete", None):
                self.open_conversation(self.current_conversation)
            self.load_conversations()
        elif kind == "listen_requested":
            if self.config.get("ui.pop_on_listen", True):
                self.pop_up(str(event.get("activation_token", "")))
        elif kind == "subscribed":
            self.refresh()

    def _set_status(self, state: str) -> None:
        label, _ = _STATUS.get(state, (state.title(), "muted"))
        self.status_label.setText(label)
        self.busy = state in ("listening", "thinking", "speaking")
        accent = str(self.config.get("ui.accent", "#7c5cff"))
        on_accent = theme.palette(str(self.config.get("ui.theme", "auto")),
                                  accent)["on_accent"]
        self._set_icon(self.mic_button, "stop" if self.busy else "mic", on_accent)
        self.mic_button.setToolTip(
            "Stop  (Escape)" if self.busy
            else "Talk to Toony (or press the global shortcut)")

    # ---- talking to the daemon --------------------------------------------
    def refresh(self) -> None:
        self.client.send("status", self._on_status, timeout=10)
        self.load_conversations()

    def _on_status(self, reply: dict) -> None:
        if not reply.get("ok"):
            self._set_status("offline")
            return
        self._set_status(str(reply.get("state", "idle")))
        conversation = str(reply.get("conversation", ""))
        if conversation and conversation != self.current_conversation:
            self.open_conversation(conversation)

    def load_conversations(self) -> None:
        self.client.send("conversations", self._on_conversations, timeout=10)

    def _on_conversations(self, reply: dict) -> None:
        if not reply.get("ok"):
            return
        self.current_conversation = (str(reply.get("current", ""))
                                     or self.current_conversation)
        self.conversation_list.clear()
        for summary in reply.get("conversations", []):
            item = QListWidgetItem(summary.get("title", "Conversation"))
            item.setData(Qt.UserRole, summary.get("id"))
            item.setToolTip(f"{summary.get('turns', 0)} turns · "
                            f"{_ago(summary.get('updated', 0))}\n"
                            f"{summary.get('preview', '')}")
            self.conversation_list.addItem(item)
            if summary.get("id") == self.current_conversation:
                self.conversation_list.setCurrentItem(item)

    def send_text(self, text: str) -> None:
        self.add_bubble(text, "user")
        self._show_thinking()
        self.client.send("ask", self._on_reply, timeout=300, text=text,
                         speak=True, conversation=self.current_conversation)

    def _on_reply(self, reply: dict) -> None:
        self._clear_thinking()
        if reply.get("ok"):
            # The event stream usually beat us here; only speak up if it did not.
            if self._live is not None:
                self._finish_live(str(reply.get("reply", "")))
            elif not self._last_was(str(reply.get("reply", ""))):
                self.add_bubble(str(reply.get("reply", "")), "toony")
            self.current_conversation = str(reply.get("conversation",
                                                      self.current_conversation))
            self.load_conversations()
        else:
            self.add_bubble(str(reply.get("error", "Something went wrong.")),
                            "tool")

    def start_listening(self) -> None:
        """The one button. It starts a turn, and stops one already running."""
        if self.busy:
            self.interrupt()
            return
        self.client.send("listen", timeout=10)

    def interrupt(self) -> None:
        """Shut it up. The most-wanted button in any voice assistant."""
        self.client.send("cancel", timeout=10)
        self._clear_thinking()
        self._set_status("idle")

    def escape(self) -> None:
        if self.busy or self.pending_confirm:
            if self.pending_confirm:
                self.answer_permission(False)
            else:
                self.interrupt()
            return
        if self.pinned:
            return          # pinned means pinned; Escape is not a trapdoor
        self.hide()

    # ---- pinned to the desktop -------------------------------------------
    def set_pinned(self, pinned: bool) -> None:
        """Keep Toony in front, on every virtual desktop, until told otherwise.

        Three things have to happen and only two of them are Qt's to give:
        the stays-on-top flag (honoured on X11, ignored by every Wayland
        compositor, which has no protocol for a client asking to be on top),
        sticky-on-all-desktops (a window-manager call, so it is attempted
        through wmctrl or kdotool and simply skipped when neither is there),
        and the part that always works — a pinned window stops hiding itself
        on Escape or on a tray click.
        """
        pinned = bool(pinned)
        self.pinned = pinned
        try:
            self.config.set("ui.pinned", pinned)
        except Exception:
            log.debug("could not save the pinned state", exc_info=True)

        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, pinned)
        if visible:
            # Changing a window flag re-creates the native window, which hides
            # it. Put it back where the user left it.
            self.show()
            self.raise_()
        self._show_pin_state()
        _sticky(self.windowTitle(), pinned)

    def _show_pin_state(self) -> None:
        colours = theme.palette(str(self.config.get("ui.theme", "auto")),
                                str(self.config.get("ui.accent", "#7c5cff")))
        self._set_icon(self.pin_button, "pin" if self.pinned else "pin_off",
                       colours["accent"] if self.pinned else colours["text"])
        self.pin_button.setToolTip(_PIN_TIP[self.pinned])
        if self.pin_button.isChecked() != self.pinned:
            self.pin_button.setChecked(self.pinned)

    def new_conversation(self) -> None:
        self.clear_messages()
        self.client.send("conversation", self._on_opened, timeout=10, action="new")

    def open_conversation(self, conversation_id: str) -> None:
        if not conversation_id:
            return
        self.client.send("transcript", self._on_opened, timeout=15,
                         id=conversation_id)

    def _on_opened(self, reply: dict) -> None:
        if not reply.get("ok"):
            return
        self.current_conversation = str(reply.get("id", ""))
        self.clear_messages()
        for entry in reply.get("transcript", []):
            role = "user" if entry.get("role") == "user" else "toony"
            if entry.get("text"):
                self.add_bubble(str(entry["text"]), role, scroll=False)
            elif entry.get("tools"):
                self.add_bubble(f"used {entry['tools']}", "tool", scroll=False)
        QTimer.singleShot(0, self._scroll_to_end)
        self.load_conversations()

    def _open_selected(self, item: QListWidgetItem) -> None:
        conversation_id = item.data(Qt.UserRole)
        if conversation_id and conversation_id != self.current_conversation:
            self.client.send("conversation", self._on_opened, timeout=15,
                             action="open", id=conversation_id)

    def _list_menu(self, position) -> None:
        item = self.conversation_list.itemAt(position)
        if item is None:
            return
        conversation_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        delete = menu.addAction("Delete conversation")
        chosen = menu.exec(self.conversation_list.mapToGlobal(position))
        if chosen is delete:
            self.client.send("conversation", lambda r: self.load_conversations(),
                             timeout=10, action="delete", id=conversation_id)
            if conversation_id == self.current_conversation:
                self.clear_messages()

    # ---- permission -------------------------------------------------------
    def show_permission(self, request_id: str, question: str) -> None:
        self.pending_confirm = request_id
        self.permission_label.setText(question)
        self.permission.setVisible(True)
        # This one matters: an unanswered question times out. If the window
        # cannot come forward, a notification has to carry it.
        self.attention(question)

    def answer_permission(self, allow: bool) -> None:
        if self.pending_confirm:
            self.client.send("confirm", timeout=10, id=self.pending_confirm,
                             allow=allow)
        self.pending_confirm = ""
        self.permission.setVisible(False)

    # ---- the message list -------------------------------------------------
    def add_bubble(self, text: str, kind: str = "toony", scroll: bool = True) -> None:
        if not text.strip():
            return
        self._add_widget(Bubble(text.strip(), kind), kind)
        if scroll:
            QTimer.singleShot(0, self._scroll_to_end)

    def _add_widget(self, bubble: Bubble, kind: str) -> Bubble:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        bubble.setMaximumWidth(max(240, int(self.width() * 0.74)))
        if kind == "user":
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        self.messages.insertLayout(self.messages.count() - 1, row)
        return bubble

    def _last_was(self, text: str) -> bool:
        for index in range(self.messages.count() - 2, -1, -1):
            item = self.messages.itemAt(index)
            layout = item.layout() if item else None
            if layout is None:
                continue
            for child in range(layout.count()):
                widget = layout.itemAt(child).widget()
                if isinstance(widget, Bubble):
                    return widget.text().strip() == text.strip()
        return False

    # ---- text arriving a token at a time ----------------------------------
    def _append_live(self, chunk: str) -> None:
        """Grow one bubble as the model writes, rather than waiting for the end."""
        if not chunk:
            return
        self._clear_thinking()
        if self._live is None:
            self._live = self._add_widget(Bubble("", "toony"), "toony")
        self._live.setText(self._live.text() + chunk)
        QTimer.singleShot(0, self._scroll_to_end)

    def _finish_live(self, text: str) -> None:
        """The final reply. Replaces whatever streamed, so the two cannot differ."""
        self._clear_thinking()
        if self._live is not None:
            if text:
                self._live.setText(text)
            elif not self._live.text().strip():
                self._live.setParent(None)
                self._live.deleteLater()
            self._live = None
            QTimer.singleShot(0, self._scroll_to_end)
            return
        self.add_bubble(text, "toony")

    def _show_thinking(self) -> None:
        self._clear_thinking()
        self._thinking = Bubble("…", "tool")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._thinking)
        row.addStretch(1)
        self.messages.insertLayout(self.messages.count() - 1, row)
        QTimer.singleShot(0, self._scroll_to_end)

    def _clear_thinking(self) -> None:
        if self._thinking is None:
            return
        self._thinking.setParent(None)
        self._thinking.deleteLater()
        self._thinking = None

    def clear_messages(self) -> None:
        self._clear_thinking()
        self._live = None
        while self.messages.count() > 1:
            item = self.messages.takeAt(0)
            layout = item.layout()
            if layout is None:
                continue
            while layout.count():
                widget = layout.takeAt(0).widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()

    def _scroll_to_end(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---- window behaviour -------------------------------------------------
    def toggle_sidebar(self) -> None:
        self.conversation_list.setVisible(not self.conversation_list.isVisible())
        if self.conversation_list.isVisible():
            self.load_conversations()

    def pop_up(self, token: str = "") -> bool:
        """Bring the window forward. Returns whether it could take focus.

        Under Wayland a client may not focus itself: `activateWindow()` and
        `raise_()` are silently ignored, and KDE is strict about it. The one
        way in is an xdg-activation token, granted by the compositor to
        whoever has focus and passed to us. `toony listen` is spawned by the
        compositor, so it holds one; it arrives here through the daemon.

        Qt spends the token from the environment, and it is single-use, so it
        is set immediately before activating and cleared straight after.
        """
        showing = self.isVisible()
        if not showing:
            self.show()
        self.raise_()

        wayland = _wayland()
        if token:
            os.environ["XDG_ACTIVATION_TOKEN"] = token
        try:
            handle = self.windowHandle()
            if handle is not None:
                handle.requestActivate()
            else:
                self.activateWindow()
        finally:
            if token:
                os.environ.pop("XDG_ACTIVATION_TOKEN", None)

        if wayland and not token:
            # Say so rather than assuming: the window is up, but behind
            # whatever has focus, and nothing we can do here changes that.
            log.debug("no activation token — the window may stay in the "
                      "background under Wayland")
            return False
        return True

    def attention(self, message: str, token: str = "") -> None:
        """Get looked at. Falls back to a notification when focus is refused."""
        if self.pop_up(token):
            return
        if self.on_attention:
            self.on_attention(message)

    def toggle_visible(self, token: str = "") -> None:
        """From the tray, so this click is our own: activation is allowed."""
        if self.isVisible() and not self.isMinimized():
            if self.pinned:
                self.pop_up(token)      # pinned: bring it forward, never away
                return
            self.hide()
        else:
            self.showNormal()
            self.pop_up(token)
            self.composer.setFocus()

    def sizeHint(self) -> QSize:
        return QSize(int(self.config.get("ui.width", 460)),
                     int(self.config.get("ui.height", 640)))

    def closeEvent(self, event) -> None:
        """Closing hides; Toony is meant to stay running all day."""
        if self.config.get("ui.tray", True):
            event.ignore()
            self.hide()
            return
        self.remember_size()
        event.accept()

    def remember_size(self) -> None:
        try:
            self.config.set("ui.width", self.width(), save=False)
            self.config.set("ui.height", self.height())
        except Exception:
            log.debug("could not save the window size", exc_info=True)


def _sticky(title: str, on: bool) -> None:
    """Ask the window manager to show this window on every virtual desktop.

    Best effort by design. Qt has no API for it, so it goes through whichever
    of wmctrl or kdotool is installed, and does nothing at all when neither is
    — the rest of pinning still works, and a missing helper is not worth an
    error in the user's face.
    """
    import shutil
    import subprocess

    if shutil.which("wmctrl"):
        argv = ["wmctrl", "-r", title, "-b",
                f"{'add' if on else 'remove'},above,sticky"]
    elif shutil.which("kdotool"):
        argv = ["kdotool", "windowstate",
                f"--{'add' if on else 'remove'}", "ABOVE", "--name", title]
    else:
        log.debug("no wmctrl or kdotool — pinning cannot reach the compositor")
        return
    try:
        subprocess.run(argv, timeout=4, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("could not ask the window manager to pin: %s", exc)


def _tool_line(event: dict) -> str:
    name = str(event.get("tool", "")).replace("_", " ")
    arguments = event.get("arguments") or {}
    detail = ", ".join(f"{k} {str(v)[:40]}" for k, v in arguments.items())
    prefix = "could not " if event.get("error") else ""
    return f"↳ {prefix}{name}" + (f" — {detail}" if detail else "")


def _ago(when: float) -> str:
    if not when:
        return "just now"
    seconds = max(0, time.time() - float(when))
    for limit, unit, name in ((60, 1, "second"), (3600, 60, "minute"),
                              (86400, 3600, "hour")):
        if seconds < limit:
            count = int(seconds // unit)
            return f"{count} {name}{'s' if count != 1 else ''} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"
