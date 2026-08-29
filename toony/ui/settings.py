"""Every setting, editable without touching the TOML file.

The form is generated from the configuration itself rather than hand-written,
so a setting added in ``config.py`` appears here automatically. :data:`HINTS`
only supplies what the value cannot say about itself — the choices for an enum,
the bounds of a number, and a sentence of explanation.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSlider,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from ..config import DEFAULTS, coerce
from ..log import get

log = get("ui.settings")

# key -> (choices | (low, high), one-line explanation)
HINTS: dict[str, tuple] = {
    "brain.provider": (["ollama", "claude", "openai"],
                       "Which model answers you. Ollama runs on this laptop."),
    "brain.claude.effort": (["low", "medium", "high", "xhigh", "max"],
                            "How hard Claude thinks. Low keeps voice snappy."),
    "brain.claude.thinking": (["adaptive", "disabled"], ""),
    "brain.temperature": ((0.0, 2.0), "Higher is more varied, lower more literal."),
    "brain.max_history_turns": ((1, 100), "How much of the conversation the "
                                          "model is shown."),
    "brain.max_tool_iterations": ((1, 20), "How many tool rounds one answer may take."),
    "stt.provider": (["local", "openai"], "Where speech is turned into text."),
    "stt.local.model": (["tiny", "base", "small", "medium", "large-v3"],
                        "Bigger is more accurate and slower."),
    "stt.local.device": (["auto", "cuda", "cpu"], "cuda uses the RTX 3050."),
    "stt.local.compute_type": (["auto", "float16", "int8_float16", "int8"], ""),
    "tts.provider": (["piper", "espeak", "openai"], "Which voice speaks."),
    "tts.speed": ((0.5, 2.0), "Speaking rate."),
    "tts.speakable": (None, "Say file paths and links the way a person would, "
                            "instead of reading them out slash by slash."),
    "tts.max_spoken_chars": ((0, 4000),
                             "Stop speaking after this much and say the rest is "
                             "on screen. 0 removes the cap."),
    "general.personality": (["plain", "friendly", "spicy", "custom"],
                            "How much of a character it is. Spicy jokes and "
                            "teases; it still answers first."),
    "general.personality_prompt": (None, "Used only when personality is custom."),
    "general.focus": (["general", "coding"],
                      "Coding adds programming-assistant guidance to the prompt."),
    "vision.enabled": (None, "Let it look at your screen."),
    "vision.provider": (["auto", "brain", "claude", "openai", "ollama"],
                        "auto uses the brain if that model can see, and the "
                        "model below if it cannot."),
    "vision.model": (None, "e.g. qwen2.5vl:7b for Ollama. Empty picks a default."),
    "wakeword.engine": (["whisper", "openwakeword"],
                        "whisper matches any phrase; openwakeword needs a "
                        "trained model but is cheaper."),
    "wakeword.phrase": (None, "What to say to wake it, with the whisper engine."),
    "wakeword.similarity": ((0.5, 0.95),
                            "Lower hears it more often, and more often wrongly."),
    "wakeword.whisper_model": (["tiny.en", "tiny", "base.en", "base"],
                               "Bigger is more accurate and uses more CPU."),
    "tools.code.root": (None, "The only folder the code tools may touch."),
    "audio.barge_in": (None, "Talk over Toony to stop it mid-sentence."),
    "audio.barge_in_sensitivity": ((1.0, 8.0),
                                   "Raise this if it interrupts itself through "
                                   "the speakers. Headphones need less."),
    "audio.barge_in_ms": ((100, 1500),
                          "How long you have to speak before it counts."),
    "brain.parallel_tools": (None, "Run independent read-only tools at the same "
                                   "time. Anything that asks first still waits."),
    "tools.max_parallel": ((1, 12), ""),
    "telegram.enabled": (None, "Message Toony from your phone. Set the token up "
                               "with: toony telegram setup"),
    "telegram.token": (None, "From @BotFather on Telegram."),
    "telegram.allowed_chats": (None, "Only these chats may drive this machine. "
                                     "Pair one with: toony telegram pair"),
    "telegram.max_message_chars": ((100, 8000),
                                   "A longer message is refused rather than "
                                   "answered."),
    "telegram.max_backlog": ((1, 200),
                             "Messages that piled up while offline, past this "
                             "many, get an apology instead of an answer."),
    "telegram.speak_replies": (None, "Also say the answer out loud on the "
                                     "laptop. Usually you are not there."),
    "audio.vad": (["energy", "webrtc"], "How the end of your sentence is detected."),
    "audio.silence_ms": ((200, 3000), "Silence that ends an utterance."),
    "audio.max_utterance_s": ((5, 120), ""),
    "audio.energy_threshold": ((0.001, 0.2), "Raise it in a noisy room."),
    "audio.vad_aggressiveness": ((0, 3), ""),
    "ptt.mode": (["toggle", "hold"], "Toggle: press to start, press to stop."),
    "ptt.shortcut": (None, "Rebind with: toony shortcut \"Meta+Space\""),
    "tools.policy_safe": (["allow", "ask", "deny"], "Reading state: volume, time."),
    "tools.policy_sensitive": (["allow", "ask", "deny"],
                               "Changing things: volume, apps, clipboard."),
    "tools.policy_dangerous": (["allow", "ask", "deny"],
                               "Closing windows, typing, shutting down."),
    "tools.confirm_timeout_s": ((5, 120), "How long a permission question waits."),
    "tools.sudo.enabled": (None, "Administrator access. Set it up with: "
                                 "toony sudo enable"),
    "ui.theme": (["auto", "dark", "light"], ""),
    "ui.opacity": ((0.35, 1.0), "How see-through the window is. Under Wayland "
                                "this tints the background rather than the "
                                "window, because Wayland has no window opacity."),
    "ui.frameless": (None, "Use Toony's own title bar. Turn it off for the "
                           "normal KDE one if dragging misbehaves."),
    "ui.orb": (None, "A floating circle whose ring shows what Toony is doing. "
                     "Click it to talk, Ctrl-drag to move it."),
    "ui.orb_size": ((48, 160), "How big the floating circle is."),
    "ui.accent": (None, "The colour of the ring while it listens, and of "
                        "buttons and selections."),
    "ui.font_size": ((10, 22), ""),
    "general.reply_word_target": ((15, 200),
                                  "Spoken answers are kept near this length."),
    "general.log_level": (["debug", "info", "warning", "error"], ""),
    "wakeword.threshold": ((0.1, 0.95), "Lower triggers more easily."),
    "conversation.resume_window_min": ((0, 1440),
                                       "Carry on the last conversation if it is "
                                       "newer than this."),
}

# Tabs, in the order they are shown. Everything else lands under "Advanced".
GROUPS = [
    ("General", ["general", "ui", "conversation"]),
    ("Brain", ["brain"]),
    ("Voice", ["stt", "tts", "audio", "wakeword", "ptt"]),
    ("Vision", ["vision"]),
    ("Phone", ["telegram"]),
    ("Skills", ["tools", "memory"]),
]

_SECRET = ("api_key", "token", "pairing_code")


class SettingsDialog(QDialog):
    """Edits the live configuration and hands the changes back on accept."""

    applied = Signal(dict)

    def __init__(self, config, parent=None, on_preview=None):
        super().__init__(parent)
        self.config = config
        self.on_preview = on_preview
        self.editors: dict[str, QWidget] = {}
        self.setWindowTitle("Toony settings")
        self.resize(620, 620)
        if parent is not None:
            self.setStyleSheet(parent.styleSheet())
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        flat = self.config.flatten()
        assigned: set[str] = set()
        for label, prefixes in GROUPS:
            keys = [k for k in flat if k.split(".")[0] in prefixes]
            assigned.update(keys)
            tabs.addTab(self._page(sorted(keys), flat), label)
        rest = sorted(set(flat) - assigned)
        if rest:
            tabs.addTab(self._page(rest, flat), "Advanced")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel
                                   | QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._reset)
        layout.addWidget(buttons)

    def _page(self, keys: list[str], flat: dict) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        host = QWidget()
        form = QFormLayout(host)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(9)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        section = None
        for key in keys:
            head = ".".join(key.split(".")[:-1])
            if head != section:
                section = head
                heading = QLabel(f"<b>{head.replace('.', ' · ')}</b>")
                heading.setContentsMargins(0, 10 if form.rowCount() else 0, 0, 2)
                form.addRow(heading)
            editor = self._editor(key, flat[key])
            self.editors[key] = editor
            hint = HINTS.get(key, (None, ""))[1]
            label = QLabel(key.split(".")[-1].replace("_", " "))
            if hint:
                label.setToolTip(hint)
                editor.setToolTip(hint)
            form.addRow(label, editor)
        area.setWidget(host)
        return area

    def _editor(self, key: str, value) -> QWidget:
        choices, _ = HINTS.get(key, (None, ""))

        if isinstance(value, bool):
            box = QCheckBox()
            box.setChecked(value)
            return box
        if isinstance(choices, list):
            combo = QComboBox()
            combo.addItems([str(c) for c in choices])
            if str(value) not in choices:
                combo.addItem(str(value))
            combo.setCurrentText(str(value))
            return combo
        if isinstance(value, float):
            if key == "ui.opacity":
                return self._opacity_slider(value)
            spin = QDoubleSpinBox()
            low, high = choices if isinstance(choices, tuple) else (0.0, 10000.0)
            spin.setRange(float(low), float(high))
            spin.setSingleStep(0.05)
            spin.setDecimals(3)
            spin.setValue(float(value))
            return spin
        if isinstance(value, int):
            spin = QSpinBox()
            low, high = choices if isinstance(choices, tuple) else (0, 1000000)
            spin.setRange(int(low), int(high))
            spin.setValue(int(value))
            return spin
        if isinstance(value, list):
            line = QLineEdit(", ".join(str(v) for v in value))
            line.setPlaceholderText("comma separated")
            return line

        line = QLineEdit(str(value))
        if key.endswith(_SECRET):
            line.setEchoMode(QLineEdit.PasswordEchoOnEdit)
            line.setPlaceholderText("leave empty to use the environment variable")
        return line

    def _opacity_slider(self, value: float) -> QWidget:
        """Opacity is the one setting worth seeing change as you drag it."""
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(35, 100)
        slider.setValue(int(round(value * 100)))
        readout = QLabel(f"{slider.value()}%")
        readout.setFixedWidth(40)

        def changed(percent: int) -> None:
            readout.setText(f"{percent}%")
            if self.on_preview:
                self.on_preview(percent / 100.0)

        slider.valueChanged.connect(changed)
        row.addWidget(slider, 1)
        row.addWidget(readout)
        host.slider = slider          # so _value() can find it again
        return host

    # ---- reading the form back -------------------------------------------
    def _value(self, key: str, editor: QWidget):
        if isinstance(editor, QCheckBox):
            return editor.isChecked()
        if isinstance(editor, QComboBox):
            return editor.currentText()
        if isinstance(editor, (QSpinBox, QDoubleSpinBox)):
            return editor.value()
        if hasattr(editor, "slider"):
            return editor.slider.value() / 100.0
        if isinstance(editor, QLineEdit):
            return coerce(editor.text(), self.config.get(key))
        return None

    def changes(self) -> dict:
        out = {}
        for key, editor in self.editors.items():
            try:
                new = self._value(key, editor)
            except ValueError as exc:
                log.warning("%s: %s", key, exc)
                continue
            if new is not None and new != self.config.get(key):
                out[key] = new
        return out

    def _save(self) -> None:
        self.applied.emit(self.changes())
        self.accept()

    def _reset(self) -> None:
        """Put the shipped defaults back into the form, without saving yet."""
        flat: dict = {}
        for section, values in DEFAULTS.items():
            for name, value in values.items():
                flat[f"{section}.{name}"] = value
        for key, editor in self.editors.items():
            if key not in flat:
                continue
            value = flat[key]
            if isinstance(editor, QCheckBox):
                editor.setChecked(bool(value))
            elif isinstance(editor, QComboBox):
                editor.setCurrentText(str(value))
            elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
                editor.setValue(value)
            elif hasattr(editor, "slider"):
                editor.slider.setValue(int(round(float(value) * 100)))
            elif isinstance(editor, QLineEdit):
                editor.setText(", ".join(str(v) for v in value)
                               if isinstance(value, list) else str(value))
