"""系统托盘常驻：空闲 / 待确认 / 待重试三态 + 数量角标 + 点击恢复窗口。

菜单只保留四项：拉取今日转科、待确认清单、待重写队列、退出。
不堆其他功能，也不提供医嘱提交或签名入口。
图标用 QPainter 画纯色圆点与数字角标，禁止 emoji。
"""
from __future__ import annotations

from functools import partial
from typing import Callable

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from itc.ui import tokens

MENU_ITEMS = ("拉取今日转科", "待确认清单", "待重写队列", "退出")
TRAY_STATES = ("idle", "awaiting", "retry")

STATE_TITLES = {
    "idle": "空闲",
    "awaiting": "待确认",
    "retry": "待重试",
}

STATE_COLORS = {
    "idle": tokens.COLORS["neutral_helper"],
    "awaiting": tokens.WARNING,
    "retry": tokens.DANGER,
}


def _badge_icon(state: str, count: int) -> QIcon:
    """纯几何托盘图标：状态色底 + 数字角标，不含 emoji。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(STATE_COLORS.get(state, STATE_COLORS["idle"])))
    painter.drawRoundedRect(QRectF(4, 8, 56, 48), 8, 8)
    # 中央一条浅色横杠，构成「清单」的极简线性表意
    painter.setBrush(QColor(tokens.COLORS["card_bg"]))
    painter.drawRoundedRect(QRectF(14, 22, 36, 4), 2, 2)
    painter.drawRoundedRect(QRectF(14, 32, 26, 4), 2, 2)
    if count > 0:
        painter.setBrush(QColor(tokens.DANGER))
        painter.drawEllipse(QRectF(38, 2, 24, 24))
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(tokens.COLORS["card_bg"]))
        painter.drawText(QRectF(38, 2, 24, 24), Qt.AlignCenter, str(min(count, 99)))
    painter.end()
    return QIcon(pm)


class TrayController(QSystemTrayIcon):
    """托盘三态控制器：数量提示、菜单、点击恢复窗口。"""

    restoreRequested = Signal()
    quitRequested = Signal()
    countsChanged = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(_badge_icon("idle", 0), parent)
        self.setObjectName("trayController")
        self.on_restore: Callable[[], None] | None = None
        self.restore_callback: Callable[[], None] | None = None
        self._handlers: dict[str, Callable[[], None]] = {}
        self._awaiting = 0
        self._retry = 0
        self._state = "idle"

        self._menu = QMenu()
        for text in MENU_ITEMS:
            action = self._menu.addAction(text)
            action.triggered.connect(partial(self._on_menu, text))
        self.setContextMenu(self._menu)
        self.activated.connect(self._on_activated)
        self.set_counts(0, 0)
        self.show()

    # -- 三态与数量 ---------------------------------------------------------
    def set_counts(self, awaiting: int = 0, retry: int = 0) -> str:
        self._awaiting = max(0, int(awaiting))
        self._retry = max(0, int(retry))
        if self._retry > 0:
            state = "retry"
        elif self._awaiting > 0:
            state = "awaiting"
        else:
            state = "idle"
        self._state = state
        badge = self._retry if state == "retry" else self._awaiting
        self.setIcon(_badge_icon(state, badge))
        self.setToolTip(
            f"{STATE_TITLES[state]} · 待确认 {self._awaiting} / 待重试 {self._retry}"
        )
        self.countsChanged.emit(self._awaiting, self._retry)
        return state

    def state(self) -> str:
        return self._state

    def counts(self) -> dict[str, int]:
        return {"awaiting": self._awaiting, "retry": self._retry}

    def badge_text(self) -> str:
        return f"待确认 {self._awaiting} / 待重试 {self._retry}"

    def tooltip_text(self) -> str:
        return self.toolTip()

    # -- 菜单 ---------------------------------------------------------------
    def menu_items(self) -> list[str]:
        return [action.text() for action in self.contextMenu().actions()]

    def set_action_handler(self, text: str, cb: Callable[[], None] | None) -> None:
        if cb is None:
            self._handlers.pop(text, None)
        else:
            self._handlers[text] = cb

    def trigger_menu(self, text: str) -> None:
        self._on_menu(text)

    def _on_menu(self, text: str, _checked: bool = False) -> None:
        cb = self._handlers.get(text)
        if callable(cb):
            cb()
        if text == "退出":
            self.quitRequested.emit()

    # -- 点击恢复窗口 -------------------------------------------------------
    def request_restore(self) -> None:
        cb = self.on_restore or self.restore_callback
        if callable(cb):
            cb()
        self.restoreRequested.emit()

    def _on_activated(self, reason) -> None:
        # 单击 / 双击 / 中键都视为恢复窗口请求；Trigger 与 DoubleClick 最常见
        if reason in (QSystemTrayIcon.Trigger,
                      QSystemTrayIcon.DoubleClick,
                      QSystemTrayIcon.MiddleClick):
            self.request_restore()
