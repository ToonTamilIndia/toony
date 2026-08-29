"""The Toony window: conversations on the left, the exchange on the right.

Everything shown here comes from the daemon over the control socket, so the
window is a view rather than a second brain — close it and the assistant keeps
working, open it mid-sentence and it catches up.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextOption
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QScrollArea,
                               QSizeGrip, QSizePolicy, QTextEdit, QVBoxLayout,
                               QWidget)

from ..log import get
from . import avatar, theme

log = get("ui.window")

_STATUS = {
    "idle": ("Ready", "muted"),
    "starting": ("Starting…", "muted"),
    "listening": ("Listening…", "accent"),
    "thinking": ("Thinking…", "accent"),
    "speaking": ("Speaking…", "good"),
    "offline": ("Not running", "danger"),
}


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setPlaceholderText("Ask Toony anything…   (Enter to send)")
        self.setAcceptRichText(False)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFixedHeight(42)
        self.textChanged.connect(self._grow)

    def _grow(self) -> None:
        height = int(self.document().size().height()) + 18
        self.setFixedHeight(max(42, min(140, height)))

    def keyPressEvent(self, event) -> None:
        enter = event.key() in (Qt.Key_Return, Qt.Key_Enter)
        if enter and not (event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier)):
            text = self.toPlainText().strip()
            if text:
                self.clear()
                self.submitted.emit(text)
            return
        super().keyPressEvent(event)


class ToonyWindow(QWidget):
    """The main window. It owns no state the daemon does not also have."""

    def __init__(self, config, client, on_settings=None, on_quit=None):
        super().__init__()
        self.config = config
        self.client = client
        self.on_settings = on_settings
        self.on_quit = on_quit
        self.current_conversation = ""
        self.pending_confirm = ""
        self.busy = False
        self._drag_from: QPoint | None = None
        self._thinking: Bubble | None = None

        self.setWindowTitle(str(config.get("general.name", "Toony")))
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(380, 420)
        self.resize(int(config.get("ui.width", 460)),
                    int(config.get("ui.height", 640)))

        self._build()
        self.apply_style()
        self._wire()

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
        header = QWidget(objectName="header")
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

        self.sidebar_button = self._icon_button("☰", "Show or hide conversations")
        self.new_button = self._icon_button("＋", "New conversation")
        self.settings_button = self._icon_button("⚙", "Settings")
        self.hide_button = self._icon_button("—", "Hide to the tray")
        self.close_button = self._icon_button("✕", "Hide to the tray")
        for button in (self.sidebar_button, self.new_button, self.settings_button,
                       self.hide_button, self.close_button):
            row.addWidget(button)
        return header

    def _icon_button(self, glyph: str, tip: str) -> QPushButton:
        button = QPushButton(glyph, objectName="icon")
        button.setToolTip(tip)
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedSize(30, 30)
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
        self.mic_button = QPushButton("🎙", objectName="mic")
        self.mic_button.setToolTip("Talk to Toony (or press the global shortcut)")
        self.mic_button.setCursor(Qt.PointingHandCursor)
        footer.addWidget(self.composer, 1)
        footer.addWidget(self.mic_button, 0, Qt.AlignBottom)

        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 4, 4)
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)

        column.addLayout(footer)
        column.addLayout(grip_row)
        return column

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
        self.mic_button.clicked.connect(self.start_listening)
        self.new_button.clicked.connect(self.new_conversation)
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
        QShortcut(QKeySequence("Ctrl+Comma"), self,
                  lambda: self.on_settings and self.on_settings())

    # ---- appearance -------------------------------------------------------
    def apply_style(self) -> None:
        accent = str(self.config.get("ui.accent", "#7c5cff"))
        self.setStyleSheet(theme.stylesheet(
            str(self.config.get("ui.theme", "auto")), accent,
            int(self.config.get("ui.font_size", 14))))
        self.setWindowOpacity(self._opacity())
        self.setWindowFlag(Qt.WindowStaysOnTopHint,
                           bool(self.config.get("ui.always_on_top", False)))
        self.avatar_label.setPixmap(avatar.circular_pixmap(
            36, str(self.config.get("ui.avatar_url", "")), accent,
            str(self.config.get("general.name", "T"))))

    def _opacity(self) -> float:
        try:
            value = float(self.config.get("ui.opacity", 0.97))
        except (TypeError, ValueError):
            return 0.97
        return min(1.0, max(0.35, value))     # never let it become invisible

    def set_opacity(self, value: float) -> None:
        self.setWindowOpacity(min(1.0, max(0.35, value)))

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
            self.add_bubble(str(event.get("text", "")), "user")
            self._show_thinking()
        elif kind == "reply":
            self._clear_thinking()
            self.add_bubble(str(event.get("text", "")), "toony")
            if event.get("conversation") != self.current_conversation:
                self.current_conversation = str(event.get("conversation", ""))
            self.load_conversations()
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
                self.pop_up()
        elif kind == "subscribed":
            self.refresh()

    def _set_status(self, state: str) -> None:
        label, _ = _STATUS.get(state, (state.title(), "muted"))
        self.status_label.setText(label)
        self.busy = state in ("listening", "thinking", "speaking")
        self.mic_button.setText("■" if self.busy else "🎙")
        self.mic_button.setToolTip(
            "Stop (Escape)" if self.busy
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
            if not self._last_was(str(reply.get("reply", ""))):
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
        self.hide()

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
        self.pop_up()

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
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        bubble = Bubble(text.strip(), kind)
        bubble.setMaximumWidth(max(240, int(self.width() * 0.74)))
        if kind == "user":
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        self.messages.insertLayout(self.messages.count() - 1, row)
        if scroll:
            QTimer.singleShot(0, self._scroll_to_end)

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

    def pop_up(self) -> None:
        """Bring the window forward without stealing focus from typing."""
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def toggle_visible(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.pop_up()
            self.composer.setFocus()

    def sizeHint(self) -> QSize:
        return QSize(int(self.config.get("ui.width", 460)),
                     int(self.config.get("ui.height", 640)))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and event.position().y() < 58:
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                return          # Wayland: only the compositor may move a window
            self._drag_from = event.globalPosition().toPoint() - self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_from = None
        super().mouseReleaseEvent(event)

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
