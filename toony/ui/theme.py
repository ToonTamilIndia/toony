"""Colours and the stylesheet.

Two palettes, one sheet. ``auto`` asks Qt what the desktop is doing, which on
Plasma means Toony follows the system Breeze light/dark setting without being
told about it.
"""

from __future__ import annotations

DARK = {
    "bg": "#16161d",
    "panel": "#1e1e28",
    "raised": "#262633",
    "border": "#33334a",
    "text": "#e8e8f0",
    "muted": "#9a9ab0",
    "bubble_user": "#2f2b52",
    "bubble_toony": "#22222e",
    "danger": "#ff6b6b",
    "good": "#4ade80",
}

LIGHT = {
    "bg": "#f6f6fa",
    "panel": "#ffffff",
    "raised": "#eeeef4",
    "border": "#dcdce6",
    "text": "#1c1c24",
    "muted": "#6a6a7c",
    "bubble_user": "#e8e4ff",
    "bubble_toony": "#f0f0f6",
    "danger": "#d13b3b",
    "good": "#1f9d55",
}


# What the ring around the orb says, at a glance, from across the room.
STATES = {
    "idle":      {"ring": "#5a5a72", "glow": "#5a5a72", "alpha": 0.45},
    "starting":  {"ring": "#8a8aa0", "glow": "#8a8aa0", "alpha": 0.35},
    "listening": {"ring": "#4ea8ff", "glow": "#4ea8ff", "alpha": 1.00},
    "thinking":  {"ring": "#ffb454", "glow": "#ffb454", "alpha": 0.95},
    "speaking":  {"ring": "#4ade80", "glow": "#4ade80", "alpha": 1.00},
    "offline":   {"ring": "#ff6b6b", "glow": "#ff6b6b", "alpha": 0.55},
}


def state_colours(state: str, accent: str = "#7c5cff") -> dict:
    colours = dict(STATES.get(state, STATES["idle"]))
    if state == "listening":
        colours["ring"] = colours["glow"] = accent
    return colours


def resolve(mode: str) -> str:
    """Turn 'auto' into 'dark' or 'light' by asking the running desktop."""
    if mode in ("dark", "light"):
        return mode
    try:
        from PySide6.QtGui import QGuiApplication, QPalette

        app = QGuiApplication.instance()
        if app is not None:
            colour = app.palette().color(QPalette.Window)
            return "dark" if colour.lightness() < 128 else "light"
    except Exception:
        pass
    return "dark"


def palette(mode: str, accent: str = "#7c5cff") -> dict[str, str]:
    colours = dict(DARK if resolve(mode) == "dark" else LIGHT)
    colours["accent"] = accent
    return colours


def rgba(colour: str, alpha: float) -> str:
    """#rrggbb plus an alpha, as CSS. Qt stylesheets understand rgba()."""
    text = colour.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        red, green, blue = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return colour
    return f"rgba({red}, {green}, {blue}, {min(1.0, max(0.0, alpha)):.3f})"


def stylesheet(mode: str, accent: str = "#7c5cff", font_size: int = 14,
               opacity: float = 1.0) -> str:
    """The sheet, with the window's translucency baked into its backgrounds.

    Wayland has no per-window opacity — `setWindowOpacity` is silently ignored —
    so the only way to be see-through there is to paint translucent colours.
    Doing it here works on every platform.
    """
    c = palette(mode, accent)
    c["font_size"] = str(font_size)
    c["small"] = str(max(10, font_size - 2))
    if opacity < 0.999:
        for key in ("bg", "panel", "raised"):
            c[key] = rgba(c[key], opacity)
    return _SHEET.format(**c)


_SHEET = """
* {{
    font-size: {font_size}px;
    color: {text};
}}
QWidget#root {{
    background: {bg};
    border: 1px solid {border};
    border-radius: 14px;
}}
QWidget#header {{
    background: {panel};
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
    border-bottom: 1px solid {border};
}}
QLabel#title {{ font-weight: 600; }}
QLabel#subtitle, QLabel#hint {{ color: {muted}; font-size: {small}px; }}

QPushButton {{
    background: {raised};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 6px 12px;
}}
QPushButton:hover  {{ background: {accent}; color: #ffffff; border-color: {accent}; }}
QPushButton:pressed{{ background: {accent}; }}
QPushButton:disabled {{ color: {muted}; background: {panel}; }}
QPushButton#icon {{
    background: transparent; border: none; padding: 4px 8px; font-size: {font_size}px;
}}
QPushButton#icon:hover {{ background: {raised}; color: {text}; }}
QPushButton#primary {{
    background: {accent}; color: #ffffff; border: none; font-weight: 600;
}}
QPushButton#danger:hover {{ background: {danger}; border-color: {danger}; }}
QPushButton#mic {{
    background: {accent}; color: #ffffff; border: none;
    border-radius: 20px; min-width: 40px; min-height: 40px; font-size: 17px;
}}
QPushButton#mic:hover {{ background: {accent}; }}

QListWidget {{
    background: {panel};
    border: none;
    border-right: 1px solid {border};
    outline: none;
    padding: 6px;
}}
QListWidget::item {{ border-radius: 8px; padding: 8px 10px; margin: 1px 2px; }}
QListWidget::item:selected {{ background: {accent}; color: #ffffff; }}
QListWidget::item:hover:!selected {{ background: {raised}; }}

QScrollArea, QScrollArea > QWidget > QWidget {{ background: {bg}; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QLabel#bubbleUser {{
    background: {bubble_user}; border-radius: 12px; padding: 9px 13px;
}}
QLabel#bubbleToony {{
    background: {bubble_toony}; border: 1px solid {border};
    border-radius: 12px; padding: 9px 13px;
}}
QLabel#bubbleTool {{
    background: transparent; color: {muted}; font-size: {small}px; padding: 1px 6px;
}}

QTextEdit#composer, QLineEdit {{
    background: {panel}; border: 1px solid {border};
    border-radius: 10px; padding: 8px 10px; selection-background-color: {accent};
}}
QTextEdit#composer:focus, QLineEdit:focus {{ border-color: {accent}; }}

QFrame#permission {{
    background: {raised}; border: 1px solid {accent}; border-radius: 10px;
}}

QTabWidget::pane {{ border: 1px solid {border}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{
    background: transparent; padding: 7px 14px; margin-right: 2px;
    border-top-left-radius: 8px; border-top-right-radius: 8px; color: {muted};
}}
QTabBar::tab:selected {{ background: {raised}; color: {text}; }}

QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {panel}; border: 1px solid {border};
    border-radius: 8px; padding: 5px 8px; min-height: 20px;
}}
QComboBox QAbstractItemView {{
    background: {panel}; border: 1px solid {border}; selection-background-color: {accent};
}}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid {border}; background: {panel};
}}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}

QSlider::groove:horizontal {{ height: 4px; background: {border}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {accent}; width: 14px; margin: -6px 0; border-radius: 7px;
}}
QToolTip {{
    background: {panel}; color: {text};
    border: 1px solid {border}; padding: 4px 6px;
}}
QDialog, QMenu {{ background: {bg}; }}
QMenu::item:selected {{ background: {accent}; color: #ffffff; }}
"""
