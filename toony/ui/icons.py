"""The icon set, as generated SVG.

Toony used emoji for its buttons, and emoji are the reason half of them looked
broken. A colour-emoji glyph is painted by the font in the font's own colours,
so ``color: #ffffff`` does nothing to it: 🎙 on the accent is a dark shape on a
blue button. Checkboxes had the mirror-image problem — the moment a stylesheet
touches ``QCheckBox::indicator`` or ``QMenu::indicator``, Qt stops drawing the
native tick, so filling the indicator with the accent and nothing else leaves a
checked box looking like a plain blue square.

Both are fixed the same way: draw the shapes ourselves, in a colour we choose.
They are SVG, so they stay sharp at any size and on any display scale, and they
are written to the cache directory as files because Qt stylesheets understand
``url(/path/to.svg)`` but not ``data:`` URIs.

Nothing here imports Qt to *make* an icon, so the files exist even before a
window does; the only Qt in this module is turning one into a :class:`QIcon`.
"""

from __future__ import annotations

import hashlib

from ..log import get
from ..paths import CACHE_DIR

log = get("ui.icons")

ICON_DIR = CACHE_DIR / "icons"

# Every shape is drawn on a 24x24 grid, stroked with round caps and joins so
# it stays legible down to the 12px submenu arrow.
_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
         'width="24" height="24" fill="none" stroke="{colour}" '
         'stroke-width="{weight}" stroke-linecap="round" '
         'stroke-linejoin="round">')

# name -> (body, default stroke width). {colour} is filled in per palette.
SHAPES: dict[str, tuple[str, float]] = {
    # A tick, for checkboxes and checked menu items.
    "check": ('<path d="M5 12.5 L10 17.5 L19 7"/>', 2.6),
    # The partially-checked state, so it never looks like an empty box.
    "dash": ('<path d="M6 12 H18"/>', 2.6),
    # The filled centre of a chosen radio button.
    "dot": ('<circle cx="12" cy="12" r="5.2" fill="{colour}" stroke="none"/>', 0),
    "chevron_down": ('<path d="M6.5 9.5 L12 15 L17.5 9.5"/>', 2.2),
    "chevron_right": ('<path d="M9.5 6.5 L15 12 L9.5 17.5"/>', 2.2),
    # Talk to Toony.
    "mic": ('<rect x="9" y="2.5" width="6" height="11" rx="3" '
            'fill="{colour}" stroke="none"/>'
            '<path d="M5.5 11 a6.5 6.5 0 0 0 13 0"/>'
            '<path d="M12 17.5 V21"/><path d="M8 21 H16"/>', 2.0),
    # Stop talking. A square, because a square is unmistakably "stop".
    "stop": ('<rect x="6.5" y="6.5" width="11" height="11" rx="2.2" '
             'fill="{colour}" stroke="none"/>', 0),
    # Send the message. The notch on the left is what makes it a paper plane
    # rather than an arrowhead.
    "send": ('<path d="M3.5 3.5 L21 12 L3.5 20.5 L8 12 Z" '
             'fill="{colour}" stroke="{colour}" stroke-width="1.4"/>', 1.4),
    # Pinned: a thumbtack pushed in, head on.
    "pin": ('<circle cx="12" cy="6" r="4.6" fill="{colour}" stroke="none"/>'
            '<rect x="10.6" y="8.6" width="2.8" height="7" rx="1.2" '
            'fill="{colour}" stroke="none"/>'
            '<path d="M9.2 15.4 H14.8 L12 22 Z" fill="{colour}" '
            'stroke="none"/>', 0),
    # Not pinned: the same tack, hollow.
    "pin_off": ('<circle cx="12" cy="6" r="4.2"/><path d="M12 10.4 V21"/>', 1.9),
    "new": ('<path d="M12 5 V19"/><path d="M5 12 H19"/>', 2.2),
    "menu": ('<path d="M4 7 H20"/><path d="M4 12 H20"/><path d="M4 17 H20"/>', 2.2),
    "settings": ('<circle cx="12" cy="12" r="3.2"/>'
                 '<path d="M12 2.6 V5"/><path d="M12 19 V21.4"/>'
                 '<path d="M2.6 12 H5"/><path d="M19 12 H21.4"/>'
                 '<path d="M5.3 5.3 L7 7"/><path d="M17 17 L18.7 18.7"/>'
                 '<path d="M18.7 5.3 L17 7"/><path d="M7 17 L5.3 18.7"/>', 1.9),
    "minimise": ('<path d="M5 12 H19"/>', 2.2),
    "close": ('<path d="M6 6 L18 18"/><path d="M18 6 L6 18"/>', 2.2),
}

_paths: dict[tuple[str, str], str | None] = {}


def source(name: str, colour: str, weight: float | None = None) -> str | None:
    """The SVG text for one shape in one colour."""
    shape = SHAPES.get(name)
    if shape is None:
        return None
    body, default_weight = shape
    head = _HEAD.format(colour=colour,
                        weight=default_weight if weight is None else weight)
    return head + body.format(colour=colour) + "</svg>"


def path(name: str, colour: str) -> str | None:
    """The shape as a file, for ``url(...)`` in a stylesheet or a QIcon.

    Returns None rather than a path that is not there: a stylesheet pointing at
    a missing file draws an empty box, which is the bug this module removes.
    """
    key = (name, colour)
    if key not in _paths:
        _paths[key] = _write(name, colour)
    return _paths[key]


def _write(name: str, colour: str) -> str | None:
    text = source(name, colour)
    if text is None:
        log.debug("no icon called %r", name)
        return None
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    target = ICON_DIR / f"{name}-{digest}.svg"
    try:
        if not target.exists():
            ICON_DIR.mkdir(parents=True, exist_ok=True)
            # Write beside it and rename, so a second Toony never reads half a
            # file and caches an icon that will not render.
            scratch = target.with_suffix(".svg.part")
            scratch.write_text(text, encoding="utf-8")
            scratch.replace(target)
    except OSError as exc:
        log.debug("could not cache the %s icon: %s", name, exc)
        return None
    return target.as_posix()


def icon(name: str, colour: str):
    """A QIcon, or None when Qt cannot render SVG on this machine."""
    file = path(name, colour)
    if file is None:
        return None
    try:
        from PySide6.QtGui import QIcon

        drawn = QIcon(file)
        return None if drawn.isNull() else drawn
    except Exception as exc:
        log.debug("could not load the %s icon: %s", name, exc)
        return None


def css(property_name: str, name: str, colour: str) -> str:
    """``image: url(/…/check-1a2b3c.svg);`` — or nothing, if it is missing."""
    file = path(name, colour)
    return f"{property_name}: url({file});" if file else ""
