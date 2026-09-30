"""The always-on-top D.O.T.S. widget (PySide6)."""
import hashlib
import html
import math
import os
import sys
import time

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                               QLayout, QMenu, QSlider, QSystemTrayIcon, QToolButton,
                               QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dots import autostart, host, store  # noqa: E402

try:
    import psutil
except ImportError:  # liveness checks are skipped without it
    psutil = None

# ---------------------------------------------------------------- theme
BG_TOP = QColor(18, 15, 12)
BG_BOTTOM = QColor(9, 8, 7)
BORDER = QColor(255, 138, 31, 70)
ORANGE = QColor(255, 138, 31)
ORANGE_HI = QColor(255, 176, 92)
TRACK = QColor(40, 32, 26)
TEXT = QColor(236, 228, 220)
MUTED = QColor(140, 128, 118)

GREEN = QColor(61, 220, 132)
BLUE = QColor(59, 158, 255)
RED = QColor(255, 77, 77)

WIDTH = 256
COLUMNS = 5
POLL_MS = 400
LIVENESS_MS = 5000
AUTO_SEEN_AFTER = 1.5   # seconds a terminal must stay focused to count as read
STALE_AFTER = 24 * 3600  # drop sessions we cannot check that went quiet this long

SANS = ["Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", "Arial"]
MONO = ["Cascadia Mono", "JetBrains Mono", "Consolas", "SF Mono", "Menlo", "monospace"]

STATE_TEXT = {
    store.IDLE: "Ready",
    store.WORKING: "Working",
    store.QUESTION: "Waiting for you",
    store.DONE: "Done",
    store.ERROR: "Error",
}

STYLE = """
QToolTip {
    background: #15110e; color: #ece4dc; border: 1px solid rgba(255,138,31,120);
    padding: 6px 8px; border-radius: 6px;
}
QMenu {
    background: #121010; color: #ece4dc; border: 1px solid rgba(255,138,31,90);
    padding: 4px; border-radius: 8px;
}
QMenu::item { padding: 5px 18px 5px 12px; border-radius: 5px; }
QMenu::item:selected { background: rgba(255,138,31,45); color: #ffb05c; }
QMenu::item:disabled { color: #6d625a; }
QMenu::separator { height: 1px; background: #2a221c; margin: 4px 6px; }
QMenu::indicator { width: 10px; height: 10px; margin-left: 4px; }
QMenu::indicator:checked { background: #ff8a1f; border-radius: 5px; }
QToolButton#chrome {
    color: #8c8076; background: transparent; border: none; border-radius: 5px;
    font-size: 12px; padding: 0px;
}
QToolButton#chrome:hover { color: #ffb05c; background: rgba(255,138,31,30); }
QToolButton#chrome:checked { color: #ff8a1f; }
QSlider::groove:horizontal { height: 4px; background: #2a211a; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #ff8a1f; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 12px; height: 12px; margin: -5px 0; border-radius: 6px;
    background: #ff8a1f; border: 2px solid #120f0c;
}
QSlider::handle:horizontal:hover { background: #ffb05c; }
QCheckBox { color: #b8aca2; spacing: 7px; }
QCheckBox::indicator {
    width: 12px; height: 12px; border-radius: 3px;
    border: 1px solid #5a4a3d; background: #1a1512;
}
QCheckBox::indicator:checked { background: #ff8a1f; border-color: #ff8a1f; }
"""


def font(families, size, weight=QFont.Weight.Normal, spacing=0.0) -> QFont:
    f = QFont()
    f.setFamilies(families)
    f.setPointSizeF(size)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def with_alpha(color: QColor, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def short_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


# ---------------------------------------------------------------- model
class SessionModel(QObject):
    """Watches ~/.dots and exposes sessions, seen marks and usage."""

    changed = Signal()
    usage_changed = Signal()

    def __init__(self):
        super().__init__()
        self.sessions = {}   # key -> session record
        self.status = {}     # key -> status line extras (context %, model)
        self.usage = {}
        self.seen = store.read_json(store.seen_path(), {}) or {}
        self._mtimes = {}
        self._usage_mtime = None

    # ---- polling
    def poll(self) -> None:
        changed = False
        files = {p.stem: p for p in store.sessions_dir().glob("*.json")}
        for key in list(self.sessions):
            if key not in files:
                self.sessions.pop(key, None)
                self.status.pop(key, None)
                self._mtimes.pop(key, None)
                changed = True
        for key, path in files.items():
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if self._mtimes.get(key) != mtime:
                rec = store.read_json(path)
                if isinstance(rec, dict) and rec.get("state"):
                    self.sessions[key] = rec
                    self._mtimes[key] = mtime
                    changed = True
            status = store.read_json(path.with_suffix(".status"))
            if isinstance(status, dict) and status != self.status.get(key):
                self.status[key] = status
                changed = True  # context % is drawn under the dot
        if changed:
            self.changed.emit()
        self._poll_usage()

    def _poll_usage(self) -> None:
        try:
            mtime = store.usage_path().stat().st_mtime_ns
        except OSError:
            return
        if mtime != self._usage_mtime:
            self._usage_mtime = mtime
            self.usage = store.read_json(store.usage_path(), {}) or {}
            self.usage_changed.emit()

    def prune_dead(self) -> None:
        """Forget sessions whose Claude process is gone (terminal closed)."""
        now = time.time()
        for key, rec in list(self.sessions.items()):
            h = rec.get("host") or {}
            pid, name = h.get("claude_pid"), h.get("claude_name")
            dead = False
            if pid and psutil is not None:
                try:
                    proc_name = psutil.Process(pid).name()
                    dead = bool(name) and proc_name.lower() != name.lower()
                except psutil.NoSuchProcess:
                    dead = True
                except psutil.Error:
                    dead = False
            elif now - rec.get("updated_at", now) > STALE_AFTER:
                dead = True
            if dead:
                self.forget(key)

    # ---- actions
    def forget(self, key: str) -> None:
        path = store.sessions_dir() / f"{key}.json"
        store.remove(path)
        store.remove(path.with_suffix(".status"))
        self.seen.pop(key, None)
        self._save_seen()
        self.poll()

    def needs_attention(self, key: str) -> bool:
        rec = self.sessions.get(key) or {}
        return (rec.get("state") in (store.DONE, store.ERROR)
                and rec.get("state_at", 0) > self.seen.get(key, 0))

    def mark_seen(self, key: str) -> None:
        if self.needs_attention(key):
            self.seen[key] = time.time()
            self._save_seen()
            self.changed.emit()

    def mark_all_seen(self) -> None:
        keys = [k for k in self.sessions if self.needs_attention(k)]
        if keys:
            now = time.time()
            for k in keys:
                self.seen[k] = now
            self._save_seen()
            self.changed.emit()

    def _save_seen(self) -> None:
        self.seen = {k: v for k, v in self.seen.items() if k in self.sessions}
        store.write_json(store.seen_path(), self.seen)

    def ordered(self) -> list:
        return sorted(self.sessions, key=lambda k: (self.sessions[k].get("created_at", 0), k))

    def look(self, key: str):
        """(color, brightness, blinking, spinning) for a session's dot."""
        state = (self.sessions.get(key) or {}).get("state")
        if state == store.QUESTION:
            return BLUE, 1.0, True, False
        if state == store.ERROR:
            return RED, 1.0, self.needs_attention(key), False
        if state == store.DONE:
            return GREEN, 1.0, self.needs_attention(key), False
        if state == store.WORKING:
            return ORANGE, 1.0, False, True
        return GREEN, 0.5, False, False

    def urgency(self) -> QColor:
        """Most urgent colour across all sessions, for the tray icon."""
        looks = [self.look(k) for k in self.sessions]
        for color in (BLUE, RED, GREEN):
            if any(c == color and blink for c, _, blink, _ in looks):
                return color
        if any(spin for *_, spin in looks):
            return ORANGE
        return MUTED


# ---------------------------------------------------------------- widgets
class Meter(QWidget):
    """One usage meter row: label, bar, percent and time to reset."""

    def __init__(self, label: str, title: str):
        super().__init__()
        self.label, self.title = label, title
        self.pct = None
        self.resets_at = None
        self.setFixedHeight(18)

    def set_window(self, window) -> None:
        window = window or {}
        resets = window.get("resets_at")
        if resets and resets < time.time():
            self.pct, self.resets_at = 0.0, None  # window rolled over since last update
        else:
            self.pct, self.resets_at = window.get("used_percentage"), resets
        if self.pct is None:
            self.setToolTip(f"<b>{self.title}</b><br>No data yet. Usage appears after Claude "
                            "Code refreshes its status line (Pro and Max plans).")
        else:
            when = time.strftime("%a %H:%M", time.localtime(self.resets_at)) if self.resets_at else "-"
            self.setToolTip(f"<b>{self.title}</b><br>{self.pct:.1f}% used<br>Resets {when}")
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        mid = h / 2

        p.setFont(font(SANS, 7.2, QFont.Weight.Bold, 1.2))
        p.setPen(MUTED)
        p.drawText(QRectF(0, 0, 40, h), Qt.AlignmentFlag.AlignVCenter, self.label)

        bar = QRectF(42, mid - 3, w - 42 - 86, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(TRACK)
        p.drawRoundedRect(bar, 3, 3)
        if self.pct:
            frac = max(0.0, min(1.0, self.pct / 100))
            fill = QRectF(bar.x(), bar.y(), max(bar.height(), bar.width() * frac), bar.height())
            grad = QLinearGradient(fill.topLeft(), fill.topRight())
            if self.pct >= 90:
                grad.setColorAt(0, QColor(255, 110, 40))
                grad.setColorAt(1, RED)
            else:
                grad.setColorAt(0, QColor(230, 100, 10))
                grad.setColorAt(1, ORANGE_HI if frac > 0.5 else ORANGE)
            p.setBrush(grad)
            p.drawRoundedRect(fill, 3, 3)

        p.setFont(font(MONO, 8, QFont.Weight.DemiBold))
        p.setPen(TEXT if self.pct is not None else MUTED)
        pct_text = "--" if self.pct is None else f"{self.pct:.0f}%"
        p.drawText(QRectF(w - 82, 0, 34, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, pct_text)

        p.setFont(font(MONO, 7.4))
        p.setPen(MUTED)
        left = short_duration(self.resets_at - time.time()) if self.resets_at else ""
        p.drawText(QRectF(w - 44, 0, 44, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, left)


class Dot(QWidget):
    """One session: a glowing dot with the project name under it."""

    clicked = Signal(str)
    menu_requested = Signal(str, QPoint)

    SIZE = QSize(46, 55)

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.label = ""
        self.context = None  # % of the context window used, from the status line
        self.look = (GREEN, 1.0, False, False)
        self.hover = False
        self.setFixedSize(self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    @property
    def animated(self) -> bool:
        return self.look[2] or self.look[3]

    def enterEvent(self, _):
        self.hover = True
        self.update()

    def leaveEvent(self, _):
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.key)

    def contextMenuEvent(self, event):
        self.menu_requested.emit(self.key, event.globalPos())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color, level, blink, spin = self.look
        t = time.monotonic()
        pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi * 1.15) if blink else 1.0
        center = QPointF(self.width() / 2, 15)

        glow_alpha = (0.55 * pulse if blink else 0.22) * level
        glow = QRadialGradient(center, 15)
        glow.setColorAt(0, with_alpha(color, glow_alpha))
        glow.setColorAt(1, with_alpha(color, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(center, 15, 15)

        core = with_alpha(color, (0.28 + 0.72 * pulse) * level)
        shine = QRadialGradient(center + QPointF(-2, -2), 8)
        shine.setColorAt(0, core.lighter(135))
        shine.setColorAt(1, core)
        p.setBrush(shine)
        p.drawEllipse(center, 6.5, 6.5)

        if spin:
            pen = QPen(with_alpha(color, 0.85), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            start = int(-(t * 360 / 1.3) % 360 * 16)
            p.drawArc(QRectF(center.x() - 10.5, center.y() - 10.5, 21, 21), start, 110 * 16)
        elif self.hover:
            p.setPen(QPen(with_alpha(TEXT, 0.35), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(center, 10.5, 10.5)

        f = font(SANS, 7.2, QFont.Weight.Medium)
        p.setFont(f)
        p.setPen(TEXT if (blink or self.hover) else MUTED)
        text = QFontMetrics(f).elidedText(self.label, Qt.TextElideMode.ElideRight, self.width() - 4)
        p.drawText(QRectF(0, 29, self.width(), 14), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, text)

        if self.context is not None:
            p.setFont(font(MONO, 6.8))
            p.setPen(RED if self.context >= 90 else ORANGE if self.context >= 70 else MUTED)
            p.drawText(QRectF(0, 42, self.width(), 12), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       f"{self.context:.0f}%")


def chrome_button(text: str, tip: str) -> QToolButton:
    b = QToolButton()
    b.setObjectName("chrome")
    b.setText(text)
    b.setToolTip(tip)
    b.setFixedSize(20, 18)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class DotsWidget(QWidget):
    def __init__(self, model: SessionModel):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("D.O.T.S.")
        self.model = model
        self.cfg = store.load_config()
        self.dots = {}
        self._fg = None
        self._fg_since = 0.0

        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        root.setContentsMargins(14, 10, 14, 12)
        root.setSpacing(6)

        # header
        header = QHBoxLayout()
        header.setSpacing(2)
        title = QLabel("D.O.T.S.")
        title.setFont(font(SANS, 8.5, QFont.Weight.Bold, 2.4))
        title.setStyleSheet("color: #ff8a1f;")
        self.summary = QLabel("")
        self.summary.setFont(font(MONO, 7.6))
        self.summary.setStyleSheet("color: #8c8076;")
        self.gear = chrome_button("⚙", "Settings")
        self.gear.setCheckable(True)
        self.gear.toggled.connect(self._toggle_settings)
        hide = chrome_button("–", "Hide (bring back from the tray icon)")
        hide.clicked.connect(self.hide)
        hide.setVisible(QSystemTrayIcon.isSystemTrayAvailable())
        header.addWidget(title)
        header.addSpacing(8)
        header.addWidget(self.summary)
        header.addStretch(1)
        header.addWidget(self.gear)
        header.addWidget(hide)
        root.addLayout(header)

        # meters
        self.five_hour = Meter("5H", "5-hour session limit")
        self.seven_day = Meter("WEEK", "Weekly limit")
        for m in (self.five_hour, self.seven_day):
            m.setFixedWidth(WIDTH - 28)
            root.addWidget(m)

        rule = QWidget()
        rule.setFixedHeight(1)
        rule.setStyleSheet("background: rgba(255,138,31,38);")
        root.addSpacing(2)
        root.addWidget(rule)

        # dots
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 2, 0, 0)
        self.grid.setHorizontalSpacing(0)
        self.grid.setVerticalSpacing(2)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        root.addWidget(self.grid_host)
        self.empty = QLabel("No Claude sessions running")
        self.empty.setFont(font(SANS, 8))
        self.empty.setStyleSheet("color: #6d625a; padding: 8px 0 4px 0;")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.empty)

        # settings
        self.settings = QWidget()
        s = QVBoxLayout(self.settings)
        s.setContentsMargins(0, 4, 0, 0)
        s.setSpacing(6)
        row = QHBoxLayout()
        lbl = QLabel("OPACITY")
        lbl.setFont(font(SANS, 7.2, QFont.Weight.Bold, 1.2))
        lbl.setStyleSheet("color: #8c8076;")
        lbl.setFixedWidth(52)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(20, 100)
        self.opacity_value = QLabel()
        self.opacity_value.setFont(font(MONO, 7.6))
        self.opacity_value.setStyleSheet("color: #ece4dc;")
        self.opacity_value.setFixedWidth(30)
        self.opacity_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.opacity.valueChanged.connect(self._set_opacity)
        row.addWidget(lbl)
        row.addWidget(self.opacity, 1)
        row.addWidget(self.opacity_value)
        s.addLayout(row)
        self.autostart_box = QCheckBox("Start with Windows" if sys.platform == "win32" else "Start on login")
        self.autostart_box.setFont(font(SANS, 8))
        self.autostart_box.setChecked(autostart.is_enabled())
        self.autostart_box.toggled.connect(self._set_autostart)
        s.addWidget(self.autostart_box)
        self.settings.setVisible(False)
        root.addWidget(self.settings)

        self.opacity.setValue(int(self.cfg.get("opacity", 92)))
        self._set_opacity(self.opacity.value())

        # timers
        self.anim = QTimer(self, interval=33, timeout=self._animate)
        self.save_timer = QTimer(self, singleShot=True, interval=400, timeout=self._save_position)
        model.changed.connect(self.refresh)
        model.usage_changed.connect(self.refresh_usage)
        QTimer(self, interval=POLL_MS, timeout=self._tick).start()
        QTimer(self, interval=30_000, timeout=self.refresh_usage).start()  # countdowns

        self.refresh()
        self.refresh_usage()
        self._restore_position()

    # ---- painting & chrome
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        grad = QLinearGradient(r.topLeft(), r.bottomLeft())
        grad.setColorAt(0, BG_TOP)
        grad.setColorAt(1, BG_BOTTOM)
        p.fillPath(path, grad)
        p.setPen(QPen(BORDER, 1))
        p.drawPath(path)
        accent = QLinearGradient(r.left() + 20, 0, r.right() - 20, 0)
        accent.setColorAt(0, with_alpha(ORANGE, 0))
        accent.setColorAt(0.5, with_alpha(ORANGE, 0.75))
        accent.setColorAt(1, with_alpha(ORANGE, 0))
        p.setPen(QPen(accent, 1.2))
        p.drawLine(QPointF(r.left() + 20, r.top() + 0.8), QPointF(r.right() - 20, r.top() + 0.8))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()

    def moveEvent(self, _):
        self.save_timer.start()

    def contextMenuEvent(self, event):
        self._main_menu().exec(event.globalPos())

    def _main_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("Mark all as read", self.model.mark_all_seen)
        menu.addSeparator()
        settings = menu.addAction("Settings")
        settings.setCheckable(True)
        settings.setChecked(self.gear.isChecked())
        settings.toggled.connect(self.gear.setChecked)
        menu.addSeparator()
        menu.addAction("Quit D.O.T.S.", QApplication.quit)
        return menu

    def _toggle_settings(self, on: bool):
        self.settings.setVisible(on)
        if on:
            self.autostart_box.blockSignals(True)
            self.autostart_box.setChecked(autostart.is_enabled())
            self.autostart_box.blockSignals(False)

    def _set_opacity(self, value: int):
        self.setWindowOpacity(value / 100)
        self.opacity_value.setText(f"{value}%")
        if self.cfg.get("opacity") != value:
            self.cfg["opacity"] = value
            self._save_cfg()

    def _set_autostart(self, on: bool):
        try:
            autostart.set_enabled(on)
        except OSError as exc:
            self.autostart_box.setToolTip(f"Could not change autostart: {exc}")

    def _restore_position(self):
        self.adjustSize()
        pos = self.cfg.get("pos")
        screen = QApplication.primaryScreen().availableGeometry()
        if pos and any(s.availableGeometry().contains(QPoint(*pos)) for s in QApplication.screens()):
            self.move(*pos)
        else:
            self.move(screen.right() - self.width() - 24, screen.top() + 24)

    def _save_position(self):
        self.cfg["pos"] = [self.x(), self.y()]
        self._save_cfg()

    def _save_cfg(self):
        cfg = store.load_config()  # keep keys other tools wrote (install.py)
        cfg.update({k: self.cfg[k] for k in ("opacity", "pos") if k in self.cfg})
        store.save_config(cfg)

    # ---- data
    def _tick(self):
        self.model.poll()
        self._auto_seen()

    def _auto_seen(self):
        """A terminal you keep focused counts as read (when it hosts one session)."""
        fg = host.foreground_window()
        if not fg or fg == int(self.winId()):
            self._fg = None
            return
        if fg != self._fg:
            self._fg, self._fg_since = fg, time.monotonic()
            return
        if time.monotonic() - self._fg_since < AUTO_SEEN_AFTER:
            return
        keys = [k for k, rec in self.model.sessions.items() if (rec.get("host") or {}).get("hwnd") == fg]
        if len(keys) == 1:
            self.model.mark_seen(keys[0])

    def refresh(self):
        order = self.model.ordered()
        if list(self.dots) != order:
            for dot in self.dots.values():
                self.grid.removeWidget(dot)
                dot.deleteLater()
            self.dots = {}
            for i, key in enumerate(order):
                dot = Dot(key)
                dot.clicked.connect(self._dot_clicked)
                dot.menu_requested.connect(self._dot_menu)
                self.grid.addWidget(dot, i // COLUMNS, i % COLUMNS)
                self.dots[key] = dot

        labels = self._labels(order)
        for key, dot in self.dots.items():
            dot.label = labels[key]
            dot.context = self.model.status.get(key, {}).get("context")
            dot.look = self.model.look(key)
            dot.setToolTip(self._tooltip(key))
            dot.update()

        self.grid_host.setVisible(bool(order))
        self.empty.setVisible(not order)
        counts = {}
        for key in order:
            color, _, blink, spin = self.model.look(key)
            if color == BLUE:
                counts["ask"] = counts.get("ask", 0) + 1
            elif blink:
                counts["new"] = counts.get("new", 0) + 1
            elif spin:
                counts["run"] = counts.get("run", 0) + 1
        self.summary.setText("  ".join(f"{n} {k}" for k, n in counts.items()) if order else "")

        if any(d.animated for d in self.dots.values()):
            if not self.anim.isActive():
                self.anim.start()
        else:
            self.anim.stop()
        self.adjustSize()
        tray = getattr(self, "tray", None)
        if tray:
            tray.setIcon(tray_icon(self.model.urgency()))

    def refresh_usage(self):
        self.five_hour.set_window(self.model.usage.get("five_hour"))
        self.seven_day.set_window(self.model.usage.get("seven_day"))
        for key, dot in self.dots.items():  # keep "3m ago" fresh
            dot.setToolTip(self._tooltip(key))

    def _labels(self, order) -> dict:
        labels, used = {}, {}
        for key in order:
            name = self.model.sessions[key].get("project") or "session"
            used[name] = used.get(name, 0) + 1
            labels[key] = name if used[name] == 1 else f"{name} {used[name]}"
        return labels

    def _tooltip(self, key: str) -> str:
        rec = self.model.sessions.get(key, {})
        status = self.model.status.get(key, {})
        state = rec.get("state")
        text = STATE_TEXT.get(state, state or "?")
        if self.model.needs_attention(key):
            text += " · unread"
        ago = short_duration(time.time() - rec.get("state_at", time.time()))
        lines = [f"<b style='color:#ffb05c'>{html.escape(rec.get('project') or 'session')}</b>",
                 f"{text} · {ago} ago"]
        if rec.get("detail"):
            lines.append(f"<i>{html.escape(str(rec['detail'])[:120])}</i>")
        extras = []
        if status.get("model"):
            extras.append(html.escape(str(status["model"])))
        if status.get("context") is not None:
            extras.append(f"context {status['context']:.0f}%")
        if extras:
            lines.append(" · ".join(extras))
        lines.append(f"<span style='color:#8c8076'>{html.escape(rec.get('cwd') or '')}</span>")
        lines.append("<span style='color:#6d625a'>Click: open terminal · right-click: more</span>")
        return "<br>".join(lines)

    def _animate(self):
        for dot in self.dots.values():
            if dot.animated:
                dot.update()

    # ---- dot actions
    def _dot_clicked(self, key: str):
        rec = self.model.sessions.get(key) or {}
        host.focus(rec.get("host") or {})
        self.model.mark_seen(key)

    def _dot_menu(self, key: str, pos: QPoint):
        rec = self.model.sessions.get(key) or {}
        menu = QMenu(self)
        head = menu.addAction(rec.get("project") or "session")
        head.setEnabled(False)
        menu.addAction("Open terminal", lambda: self._dot_clicked(key))
        read = menu.addAction("Mark as read", lambda: self.model.mark_seen(key))
        read.setEnabled(self.model.needs_attention(key))
        menu.addAction("Remove dot", lambda: self.model.forget(key))
        menu.exec(pos)


# ---------------------------------------------------------------- app
def tray_icon(color: QColor) -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(12, 10, 9))
    p.drawEllipse(2, 2, 60, 60)
    p.setPen(QPen(ORANGE, 5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(6, 6, 52, 52)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawEllipse(20, 20, 24, 24)
    p.end()
    return QIcon(pix)


def single_instance(name: str, on_message) -> bool:
    """True if we are the first instance; otherwise pokes the running one."""
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        return False
    QLocalServer.removeServer(name)
    server = QLocalServer(QApplication.instance())
    server.newConnection.connect(lambda: (server.nextPendingConnection(), on_message()))
    server.listen(name)
    return True


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("D.O.T.S.")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)

    holder = {}
    instance = "dots-" + hashlib.sha1(str(store.home()).encode()).hexdigest()[:12]
    if not single_instance(instance, lambda: holder["show"]()):
        return

    model = SessionModel()
    model.poll()
    model.prune_dead()
    widget = DotsWidget(model)

    def show():
        widget.show()
        widget.raise_()
    holder["show"] = show

    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(tray_icon(model.urgency()), app)
        tray.setToolTip("D.O.T.S.")
        menu = QMenu()
        menu.addAction("Show / hide", lambda: widget.setVisible(not widget.isVisible()))
        menu.addAction("Mark all as read", model.mark_all_seen)
        menu.addSeparator()
        menu.addAction("Quit", app.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: reason == QSystemTrayIcon.ActivationReason.Trigger
                               and widget.setVisible(not widget.isVisible()))
        tray.show()
        widget.tray = tray

    QTimer(app, interval=LIVENESS_MS, timeout=model.prune_dead).start()
    show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
