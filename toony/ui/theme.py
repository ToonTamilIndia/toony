"""Colours and the stylesheet.

Two palettes, one sheet. ``auto`` asks Qt what the desktop is doing, which on
Plasma means Toony follows the system Breeze light/dark setting without being
told about it.

The accent is chosen by the user and may be anything, so nothing here hardcodes
what goes *on* the accent: :func:`contrast_on` picks black or white per accent,
and every rule that paints the accent as a background uses it. That is what
stops a pale accent swallowing white button text, and a dark one swallowing
black.
"""

from __future__ import annotations

from . import icons

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


# ---- colour arithmetic ----------------------------------------------------

def channels(colour: str) -> tuple[int, int, int] | None:
    """(r, g, b) from #rgb or #rrggbb, or None if it is not a hex colour."""
    text = colour.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore
    except ValueError:
        return None


def luminance(colour: str) -> float:
    """WCAG relative luminance, 0 (black) to 1 (white)."""
    rgb = channels(colour)
    if rgb is None:
        return 0.5
    parts = []
    for value in rgb:
        fraction = value / 255.0
        parts.append(fraction / 12.92 if fraction <= 0.04045
                     else ((fraction + 0.055) / 1.055) ** 2.4)
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast_on(colour: str) -> str:
    """The readable foreground for text sitting on ``colour``.

    0.42 rather than 0.5: black on a mid colour reads better than white does,
    so the switch to white happens a little later than the midpoint.
    """
    return "#12121a" if luminance(colour) > 0.42 else "#ffffff"


def mix(first: str, second: str, amount: float) -> str:
    """``amount`` of the way from ``first`` to ``second``."""
    one, two = channels(first), channels(second)
    if one is None or two is None:
        return first
    amount = min(1.0, max(0.0, amount))
    blend = tuple(round(a + (b - a) * amount) for a, b in zip(one, two))
    return "#%02x%02x%02x" % blend


def palette(mode: str, accent: str = "#7c5cff") -> dict[str, str]:
    colours = dict(DARK if resolve(mode) == "dark" else LIGHT)
    colours["accent"] = accent
    # Everything that lands on top of the accent, derived from the accent
    # itself so a user-chosen colour can never make its own labels vanish.
    colours["on_accent"] = contrast_on(accent)
    colours["accent_hover"] = mix(accent, "#ffffff", 0.16)
    colours["accent_press"] = mix(accent, "#000000", 0.16)
    # A tint of the accent, for the "this toggle is on" background.
    colours["accent_soft"] = mix(colours["panel"], accent, 0.26)
    colours["on_danger"] = contrast_on(colours["danger"])
    return colours


def rgba(colour: str, alpha: float) -> str:
    """#rrggbb plus an alpha, as CSS. Qt stylesheets understand rgba()."""
    rgb = channels(colour)
    if rgb is None:
        return colour
    red, green, blue = rgb
    return f"rgba({red}, {green}, {blue}, {min(1.0, max(0.0, alpha)):.3f})"


# ---- the parts of the sheet that need artwork -----------------------------

def _indicators(c: dict[str, str]) -> str:
    """Checkbox, radio and menu ticks.

    Qt drops the native tick as soon as a stylesheet styles an indicator, so
    these rules are all-or-nothing: without the artwork we style nothing and
    let the platform draw its own, rather than leaving a blue square with no
    mark in it.
    """
    tick_on_accent = icons.css("image", "check", c["on_accent"])
    tick_on_panel = icons.css("image", "check", c["text"])
    dash = icons.css("image", "dash", c["on_accent"])
    dot = icons.css("image", "dot", c["on_accent"])
    if not (tick_on_accent and tick_on_panel):
        return "/* no icon cache — leaving indicators to the native style */"
    return f"""
QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 18px; height: 18px;
    border: 1px solid {c['border']};
    background: {c['panel']};
}}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {c['accent']};
}}
QCheckBox::indicator:checked {{
    background: {c['accent']}; border-color: {c['accent']}; {tick_on_accent}
}}
QCheckBox::indicator:indeterminate {{
    background: {c['accent']}; border-color: {c['accent']}; {dash}
}}
QRadioButton::indicator:checked {{
    background: {c['accent']}; border-color: {c['accent']}; {dot}
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background: {c['raised']}; border-color: {c['border']};
}}
QMenu::indicator {{ width: 16px; height: 16px; left: 10px; }}
QMenu::indicator:checked, QMenu::indicator:non-exclusive:checked,
QMenu::indicator:exclusive:checked {{ {tick_on_panel} }}
QMenu::indicator:checked:selected,
QMenu::indicator:non-exclusive:checked:selected,
QMenu::indicator:exclusive:checked:selected {{ {tick_on_accent} }}
"""


def _arrows(c: dict[str, str]) -> str:
    """Combo box and submenu arrows, in a colour that suits the palette."""
    down = icons.css("image", "chevron_down", c["muted"])
    right = icons.css("image", "chevron_right", c["muted"])
    right_selected = icons.css("image", "chevron_right", c["on_accent"])
    if not (down and right):
        return ""
    return f"""
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ width: 12px; height: 12px; {down} }}
QMenu::right-arrow {{ width: 12px; height: 12px; margin-right: 8px; {right} }}
QMenu::right-arrow:selected {{ {right_selected} }}
"""


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
    c["indicators"] = _indicators(c)
    c["arrows"] = _arrows(c)
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
QPushButton:hover {{
    background: {accent_hover}; color: {on_accent}; border-color: {accent_hover};
}}
QPushButton:pressed {{
    background: {accent_press}; color: {on_accent}; border-color: {accent_press};
}}
QPushButton:disabled {{
    color: {muted}; background: {panel}; border-color: {border};
}}
QPushButton#icon {{
    background: transparent; border: none; padding: 4px; border-radius: 8px;
}}
QPushButton#icon:hover {{ background: {raised}; color: {text}; }}
QPushButton#icon:pressed {{ background: {border}; color: {text}; }}
QPushButton#icon:checked {{ background: {accent_soft}; color: {text}; }}

/* Anything painted in the accent takes its foreground from the accent, so a
   user-chosen colour can never hide its own label. */
QPushButton#primary, QPushButton#mic, QPushButton#send, QPushButton:default {{
    background: {accent}; color: {on_accent}; border: 1px solid {accent};
    font-weight: 600;
}}
QPushButton#primary:hover, QPushButton#mic:hover, QPushButton#send:hover,
QPushButton:default:hover {{
    background: {accent_hover}; border-color: {accent_hover}; color: {on_accent};
}}
QPushButton#primary:pressed, QPushButton#mic:pressed, QPushButton#send:pressed,
QPushButton:default:pressed {{
    background: {accent_press}; border-color: {accent_press}; color: {on_accent};
}}
QPushButton#primary:disabled, QPushButton#mic:disabled, QPushButton#send:disabled,
QPushButton:default:disabled {{
    background: {raised}; color: {muted}; border-color: {border};
}}
QPushButton#mic, QPushButton#send {{
    border-radius: 20px; min-width: 40px; max-width: 40px;
    min-height: 40px; max-height: 40px; padding: 0;
}}
QPushButton#danger {{ color: {danger}; }}
QPushButton#danger:hover {{
    background: {danger}; color: {on_danger}; border-color: {danger};
}}
QDialogButtonBox QPushButton {{ min-width: 88px; }}

QListWidget {{
    background: {panel};
    border: none;
    border-right: 1px solid {border};
    outline: none;
    padding: 6px;
}}
QListWidget::item {{ border-radius: 8px; padding: 8px 10px; margin: 1px 2px; }}
QListWidget::item:selected {{ background: {accent}; color: {on_accent}; }}
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
    selection-color: {on_accent};
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
QTabBar::tab:hover:!selected {{ color: {text}; }}
QTabBar::tab:selected {{ background: {raised}; color: {text}; }}

QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {panel}; border: 1px solid {border};
    border-radius: 8px; padding: 5px 8px; min-height: 20px;
}}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {accent}; }}
QComboBox QAbstractItemView {{
    background: {panel}; border: 1px solid {border};
    selection-background-color: {accent}; selection-color: {on_accent};
    outline: none;
}}
{indicators}
{arrows}

QSlider::groove:horizontal {{ height: 4px; background: {border}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {accent}; width: 14px; margin: -6px 0; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QToolTip {{
    background: {panel}; color: {text};
    border: 1px solid {border}; padding: 4px 6px;
}}
QDialog {{ background: {bg}; }}

/* The tray and orb menu. It is the only Toony surface some sessions ever see,
   so it gets the same care as the window. */
QMenu {{
    background: {panel};
    border: 1px solid {border};
    border-radius: 10px;
    padding: 6px 4px;
}}
QMenu::item {{
    padding: 7px 24px 7px 34px;
    border-radius: 7px;
    margin: 1px 4px;
    color: {text};
}}
QMenu::item:selected {{ background: {accent}; color: {on_accent}; }}
QMenu::item:disabled {{ color: {muted}; background: transparent; }}
QMenu::icon {{ left: 10px; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 5px 12px; }}
QMenu#status::item:disabled {{ font-weight: 600; color: {muted}; }}
"""
