"""The orb: a small circle that lives on your desktop and shows what Toony is doing.

The window is for reading; this is for glancing. A ring around the avatar
carries the whole state — grey and still when idle, your accent colour sweeping
round while it listens, amber turning over while it thinks, green breathing
while it talks — so you can tell from across the room whether it heard you.

Everything is drawn rather than themed, because a Qt stylesheet cannot describe
a rotating arc, and animation is what makes the difference between "a coloured
dot" and something that feels alive.
"""

from __future__ import annotations

import math
import os

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QPainter, QPainterPath, QPen,
                           QRadialGradient)
from PySide6.QtWidgets import QMenu, QWidget

from ..log import get
from . import avatar, theme

log = get("ui.orb")

FPS = 30
# How much room the ring and its glow need outside the avatar itself.
MARGIN = 14


class Orb(QWidget):
    """A draggable circle showing Toony's state. Click it to talk."""

    clicked = Signal()
    opened = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.state = "idle"
        self._phase = 0.0
        self._level = 0.0          # 0..1, eases toward the target for the state
        self._press: QPoint | None = None
        self._moved = False

        self.diameter = int(config.get("ui.orb_size", 76))
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_AlwaysShowToolTips, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Toony — click to talk, drag to move")
        self.setFixedSize(self.diameter + MARGIN * 2, self.diameter + MARGIN * 2)

        self._avatar = None
        self.reload_avatar()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(1000 // FPS)

    # ---- appearance -------------------------------------------------------
    def reload_avatar(self) -> None:
        self._avatar = avatar.circular_pixmap(
            self.diameter, str(self.config.get("ui.avatar_url", "")),
            str(self.config.get("ui.accent", "#7c5cff")),
            str(self.config.get("general.name", "T")))
        self.update()

    def set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        # Idle needs no animation at all; leaving a timer running to redraw a
        # static circle thirty times a second is not free on a laptop battery.
        if state in ("listening", "thinking", "speaking"):
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 1.0 / FPS) % 60.0
        target = 1.0 if self.state in ("listening", "speaking") else 0.6
        self._level += (target - self._level) * 0.18
        self.update()

    # ---- painting ---------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        centre = self.rect().center()
        radius = self.diameter / 2
        colours = theme.state_colours(
            self.state, str(self.config.get("ui.accent", "#7c5cff")))
        ring = QColor(colours["ring"])
        alpha = colours["alpha"]

        self._paint_glow(painter, centre, radius, ring, alpha)
        self._paint_avatar(painter, centre, radius)
        self._paint_ring(painter, centre, radius, ring, alpha)
        painter.end()

    def _paint_glow(self, painter, centre, radius, ring, alpha) -> None:
        """A soft halo. It is what makes the state readable at a glance."""
        strength = alpha * (0.35 + 0.25 * self._pulse())
        if strength <= 0.05:
            return
        outer = radius + MARGIN
        gradient = QRadialGradient(centre, outer)
        inner = QColor(ring)
        inner.setAlphaF(min(1.0, strength))
        edge = QColor(ring)
        edge.setAlphaF(0.0)
        gradient.setColorAt(radius / outer * 0.98, inner)
        gradient.setColorAt(1.0, edge)
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(QRectF(centre.x() - outer, centre.y() - outer,
                                   outer * 2, outer * 2))

    def _paint_avatar(self, painter, centre, radius) -> None:
        if self._avatar is None or self._avatar.isNull():
            return
        box = QRectF(centre.x() - radius, centre.y() - radius,
                     radius * 2, radius * 2)
        clip = QPainterPath()
        clip.addEllipse(box)
        painter.save()
        painter.setClipPath(clip)
        painter.drawPixmap(box.toRect(), self._avatar)
        painter.restore()

    def _paint_ring(self, painter, centre, radius, ring, alpha) -> None:
        width = 3.0 + 1.5 * self._pulse() if self.state != "idle" else 2.0
        inset = radius + 4
        box = QRectF(centre.x() - inset, centre.y() - inset, inset * 2, inset * 2)

        # The full circle, faint, so the ring reads as a ring and not an arc.
        faint = QColor(ring)
        faint.setAlphaF(alpha * 0.22)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(faint, width, Qt.SolidLine, Qt.RoundCap))
        painter.drawEllipse(box)

        span = self._span()
        if span <= 0:
            return
        bright = QColor(ring)
        bright.setAlphaF(alpha)
        painter.setPen(QPen(bright, width, Qt.SolidLine, Qt.RoundCap))
        # Qt measures arcs in sixteenths of a degree, anticlockwise from 3 o'clock.
        painter.drawArc(box, int(-self._start() * 16), int(-span * 16))

    # ---- the shape of each state -----------------------------------------
    def _pulse(self) -> float:
        """0..1, breathing. Slow while speaking, quicker while listening."""
        rate = {"listening": 2.2, "thinking": 1.6, "speaking": 1.1}.get(
            self.state, 0.6)
        return (math.sin(self._phase * rate * math.pi) + 1) / 2

    def _start(self) -> float:
        """Where the bright arc begins, in degrees clockwise from the top."""
        if self.state == "thinking":
            return (self._phase * 240) % 360 - 90
        if self.state == "listening":
            return -90 - self._span() / 2      # centred at the top, growing out
        return -90

    def _span(self) -> float:
        if self.state == "idle":
            return 0.0
        if self.state == "thinking":
            return 90.0                        # a chasing quarter
        if self.state == "listening":
            return 40.0 + 260.0 * self._level * self._pulse()
        if self.state == "speaking":
            return 360.0                       # whole ring, breathing
        return 120.0

    # ---- interaction ------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            return
        self._moved = False
        handle = self.windowHandle()
        if event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier:
            # Ctrl-drag to move: a plain drag would fight the click-to-talk.
            if handle is not None and handle.startSystemMove():
                self._moved = True
                return
        self._press = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:
        if self._press is None or not (event.buttons() & Qt.LeftButton):
            return
        moved = (event.globalPosition().toPoint() - self._press) - self.pos()
        if abs(moved.x()) + abs(moved.y()) < 6:
            return                              # a shaky click is still a click
        self._moved = True
        handle = self.windowHandle()
        if handle is not None and handle.startSystemMove():
            self._press = None
            return
        self.move(event.globalPosition().toPoint() - self._press)

    def mouseReleaseEvent(self, event) -> None:
        was_drag, self._moved, self._press = self._moved, False, None
        if event.button() == Qt.LeftButton and not was_drag:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, event) -> None:
        self.opened.emit()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(theme.stylesheet(
            str(self.config.get("ui.theme", "auto")),
            str(self.config.get("ui.accent", "#7c5cff"))))
        self.build_menu(menu)
        menu.exec(QCursor.pos())

    def build_menu(self, menu: QMenu) -> None:
        """Filled in by whoever created the orb; kept here so both menus match."""
        menu.addAction("Open Toony", self.opened.emit)

    def show_at_corner(self) -> None:
        """Bottom right of the current screen, where it is least in the way.

        Wayland ignores this — a client may not place itself — so it lands
        wherever the compositor puts it and you drag it once with Ctrl.
        """
        screen = self.screen()
        if screen is None:
            self.show()
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 28,
                  area.bottom() - self.height() - 28)
        self.show()


def build(config, parent=None) -> Orb | None:
    if not config.get("ui.orb", True):
        return None
    return Orb(config, parent)
