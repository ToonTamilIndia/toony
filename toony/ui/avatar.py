"""Toony's face.

The picture is fetched once from the URL in ``ui.avatar_url`` and cached, so
the window opens instantly and still works with no network. If it cannot be
fetched at all, a lettered disc is drawn instead — the window should never come
up with a hole where the avatar goes.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from ..log import get
from ..paths import AVATAR_FILE

log = get("ui.avatar")

_TIMEOUT = 8.0
_MAX_BYTES = 4 * 1024 * 1024


def download(url: str, destination=None, force: bool = False):
    """Cache the avatar on disk and return its path, or None if unavailable."""
    destination = destination or AVATAR_FILE
    if destination.exists() and not force and destination.stat().st_size > 0:
        return destination
    if not url.startswith("https://"):
        log.warning("refusing to fetch an avatar over %s", url.split(":", 1)[0])
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Toony"})
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            data = response.read(_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.info("could not fetch the avatar: %s", exc)
        return None
    if not data or len(data) > _MAX_BYTES:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    log.info("cached the avatar at %s", destination)
    return destination


# ---- Qt rendering ---------------------------------------------------------
# Qt is imported inside each function so this module stays importable (and
# testable) on a machine with no Qt at all. Everything is drawn onto a QImage
# rather than a QPixmap: a QPixmap needs a live QGuiApplication, and the icon is
# also rendered from `toony install`, where there is no application and no
# display at all.

def circular_image(size: int = 64, url: str = "", accent: str = "#7c5cff",
                   letter: str = "T", allow_fallback: bool = True):
    """A round avatar as a QImage, or None if there is nothing to draw."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QPainter,
                               QPainterPath)

    source = None
    path = download(url) if url else (AVATAR_FILE if AVATAR_FILE.exists() else None)
    if path is not None:
        loaded = QImage(str(path))
        if not loaded.isNull():
            source = loaded
    if source is None and not allow_fallback:
        return None

    target = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    target.fill(Qt.transparent)
    painter = QPainter(target)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)

    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, size, size))
    painter.setClipPath(clip)

    if source is not None:
        scaled = source.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                               Qt.SmoothTransformation)
        # Centre the crop, so a non-square source is not squashed.
        painter.drawImage(-(scaled.width() - size) // 2,
                          -(scaled.height() - size) // 2, scaled)
    else:
        painter.fillRect(0, 0, size, size, QBrush(QColor(accent)))
        painter.setPen(QColor("#ffffff"))
        font = QFont()
        font.setPixelSize(int(size * 0.5))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter,
                         (letter or "T")[:1].upper())
    painter.end()
    return target


def circular_pixmap(size: int = 64, url: str = "", accent: str = "#7c5cff",
                    letter: str = "T"):
    """A round avatar ready to put in a label. Needs a running QApplication."""
    from PySide6.QtGui import QPixmap

    return QPixmap.fromImage(circular_image(size, url, accent, letter))


def window_icon(url: str = "", accent: str = "#7c5cff"):
    from PySide6.QtGui import QIcon

    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(circular_pixmap(size, url, accent))
    return icon


def save_icon_file(url: str, destination, size: int = 256) -> bool:
    """Write a round PNG for the desktop entry and the tray to point at.

    Called from `toony install`, with no display and no QApplication, so it
    draws nothing unless there is a real picture to crop — a lettered disc
    would need font machinery that is not available there.
    """
    try:
        image = circular_image(size, url, allow_fallback=False)
    except Exception as exc:
        log.info("could not render the icon: %s", exc)
        return False
    if image is None:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    return bool(image.save(str(destination), "PNG"))
