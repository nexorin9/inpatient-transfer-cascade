"""界面状态与打磨：加载 / 空 / 错误三态、toast、破坏性操作二次确认、两档宽度。

只使用 PySide6 2D 控件与 QPainter 画几何图标，不用 emoji、不用动画库、不用 Web 视图。
所有配色取自 ui.tokens，样式不散落在各界面文件里。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from PySide6.QtCore import Qt, QPoint, QRectF, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from itc.ui import tokens

# 三种空态文案（任务 45）：今日无转科、无牵动项、清单为空。
EMPTY_COPY = {
    "today": ("今日暂无转科单", "把转科单 JSON 拖入窗口，或粘贴 JSON 文本开始"),
    "scope": ("本次转科无牵动项", "未发现需要连带处置的执行医嘱 / 待送标本 / 床位费"),
    "list": ("处置清单为空", "筛选条件下没有条目，切回「全部」查看"),
}

# 加载三阶段文案：取数 / 判定 / 写回，期间禁止重复点击。
LOADING_COPY = {
    "fetch": "正在取数，请稍候…",
    "judge": "正在判定牵动范围，请稍候…",
    "writeback": "正在逐条回写，请稍候…",
}

# 错误态文案与处置建议，区分可重试与不可重试。
ERROR_COPY = {
    "his": ("HIS 不可达", "检查网络与 HIS 服务地址后可重试", True),
    "model": ("模型服务不可达", "规则可判项仍可逐条确认，语义项转为待人工判定", False),
    "writeback": ("写回失败", "已转入待重写队列，保留同一幂等键重试", True),
    "import": ("转科单字段不合规", "按下方行级提示补齐必填字段后重新导入", True),
}

THREE_STATES = ("loading", "empty", "error")


def _line_icon(kind: str, color: str) -> QPixmap:
    """用 QPainter 画线性几何图标位：圆 / 卡片 / 警示三角，均不含 emoji。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    pen = QPen(QColor(color))
    pen.setWidth(2)
    painter.setPen(pen)
    if kind == "loading":
        painter.drawEllipse(12, 12, 40, 40)
        painter.drawArc(12, 12, 40, 40, 30 * 16, 120 * 16)
    elif kind == "empty":
        painter.drawRoundedRect(QRectF(10, 16, 44, 32), 4, 4)
        painter.drawLine(10, 26, 54, 26)
        painter.drawLine(18, 36, 38, 36)
    else:
        # 警示三角 + 感叹号，纯几何与文本
        painter.drawLine(32, 8, 60, 56)
        painter.drawLine(60, 56, 4, 56)
        painter.drawLine(4, 56, 32, 8)
        painter.drawText(QRectF(26, 30, 12, 20), Qt.AlignCenter, "!")
    painter.end()
    return pm


class StatePlaceholder(QWidget):
    """内容区三态占位：loading / empty / error 共用同一容器切换。"""

    LOADING, EMPTY, ERROR = THREE_STATES

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statePlaceholder")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(tokens.SPACING[3])
        layout.setContentsMargins(24, 48, 24, 48)

        self.icon_label = QLabel()
        self.icon_label.setObjectName("stateIcon")
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.title_label = QLabel()
        self.title_label.setObjectName("stateTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet(tokens.STATE_TITLE_QSS)
        self.detail_label = QLabel()
        self.detail_label.setObjectName("stateDetail")
        self.detail_label.setAlignment(Qt.AlignCenter)
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet(tokens.STATE_DETAIL_QSS)
        self.action_button = QPushButton("重试")
        self.action_button.setObjectName("stateAction")
        self.action_button.setProperty("variant", "secondary")
        self.action_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.action_button.clicked.connect(self._on_action)

        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        row.addWidget(self.action_button)

        layout.addWidget(self.icon_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.detail_label)
        layout.addLayout(row)

        self._state = ""
        self._busy = False
        self.on_retry = None
        self.retry_callback = None
        self.show_empty("today")

    # -- 对外状态查询 -------------------------------------------------------
    def state(self) -> str:
        return self._state

    def is_busy(self) -> bool:
        return self._busy

    def set_busy(self, busy: bool) -> None:
        """加载期间禁用主操作按钮，禁止重复点击。"""
        self._busy = bool(busy)
        self.setProperty("loading", "true" if self._busy else "false")
        self.action_button.setEnabled(not self._busy)
        self.style().polish(self)

    def _apply(self, state: str, icon: str, color: str,
               title: str, detail: str, show_action: bool,
               action_enabled: bool) -> None:
        self._state = state
        self.icon_label.setPixmap(_line_icon(icon, color))
        self.title_label.setText(title)
        self.detail_label.setText(detail)
        self.action_button.setVisible(show_action)
        self.action_button.setEnabled(action_enabled and not self._busy)
        self.setObjectName("statePlaceholder")
        self.setProperty("state", state)

    def show_loading(self, phase: str = "fetch", detail: str = "") -> None:
        """加载态：取数 / 判定 / 写回三个阶段的进度提示，操作按钮禁用。"""
        title = LOADING_COPY.get(phase, str(phase))
        self._busy = True
        self._apply(
            "loading", "loading", tokens.PRIMARY, title,
            detail or "处理进行中，暂不重复提交操作", True, False,
        )
        self.setProperty("loading", "true")
        self.action_button.setEnabled(False)

    def show_empty(self, key: str = "today", hint: str = "") -> None:
        """空态：三种文案 + 大幅留白，不伪造数据。"""
        title, default_hint = EMPTY_COPY.get(key, (str(key), ""))
        self._busy = False
        self._apply(
            "empty", "empty", tokens.COLORS["neutral_disabled"], title,
            hint or default_hint, False, False,
        )
        self.setProperty("loading", "false")

    def show_error(self, kind: str = "his", retryable: bool | None = None,
                   suggestion: str = "") -> None:
        """错误态：区分可重试与不可重试，给出处置建议。"""
        if kind in ERROR_COPY:
            title, default_suggestion, default_retryable = ERROR_COPY[kind]
            can_retry = default_retryable if retryable is None else bool(retryable)
            detail = suggestion or default_suggestion
        else:
            title = str(kind)
            can_retry = False if retryable is None else bool(retryable)
            detail = suggestion
        tip = "可重试" if can_retry else "不可自动重试，请人工介入"
        self._busy = False
        self._apply(
            "error", "error", tokens.DANGER, title,
            f"{detail}（{tip}）", True, can_retry,
        )
        self.setProperty("loading", "false")

    def _on_action(self) -> None:
        cb = self.retry_callback or self.on_retry
        if callable(cb):
            cb()


# ---------------------------------------------------------------------------
# toast 反馈
# ---------------------------------------------------------------------------

_TOAST_HISTORY: list[str] = []
_ACTIVE_TOASTS: list[QFrame] = []


def toast_history() -> tuple[str, ...]:
    return tuple(_TOAST_HISTORY)


def clear_toast_history() -> None:
    _TOAST_HISTORY.clear()


def show_toast(parent: QWidget, text: str, duration_ms: int = 2500) -> QFrame:
    """右下角轻量反馈条：非模态、不阻塞、不调用事件循环。"""
    _TOAST_HISTORY.append(str(text))
    toast = QFrame(parent)
    toast.setObjectName("toast")
    toast.setFrameShape(QFrame.StyledPanel)
    toast.setStyleSheet(tokens.TOAST_QSS)
    row = QHBoxLayout(toast)
    row.setContentsMargins(12, 8, 12, 8)
    label = QLabel(str(text), toast)
    label.setObjectName("toastText")
    label.setWordWrap(True)
    row.addWidget(label)
    toast.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    toast.adjustSize()
    if parent is not None and parent.width() > 0:
        toast.move(max(0, parent.width() - toast.width() - 24),
                   max(0, parent.height() - toast.height() - 24))
    toast.show()
    # 无头平台不支持窗口提升，跳过以免产生平台告警
    if os.environ.get("QT_QPA_PLATFORM", "").lower() != "offscreen":
        toast.raise_()
    _ACTIVE_TOASTS.append(toast)
    if duration_ms > 0:
        QTimer.singleShot(duration_ms, toast.hide)
    return toast


# ---------------------------------------------------------------------------
# 破坏性操作二次确认（测试可注入自动应答）
# ---------------------------------------------------------------------------

_AUTO_ANSWER: bool | None = None


def set_auto_answer(value: bool | None) -> None:
    """注入自动应答：None 表示恢复真实交互（无头环境按安全默认拒绝）。"""
    global _AUTO_ANSWER
    _AUTO_ANSWER = None if value is None else bool(value)


@contextmanager
def auto_answer(value: bool) -> Iterator[None]:
    """作用域内注入自动应答，退出后恢复原设置。"""
    previous = _AUTO_ANSWER
    set_auto_answer(value)
    try:
        yield
    finally:
        set_auto_answer(previous)


def confirm_destructive(parent: QWidget | None, text: str) -> bool:
    """二次确认：清空清单 / 批量重跑 / 退出未确认项等破坏性操作先用它把关。

    有注入应答时直接返回；无头环境下没有可交互的模态，按安全默认拒绝（False），
    不阻塞测试线程；有头环境才弹出 QMessageBox 让用户决定。
    """
    if _AUTO_ANSWER is not None:
        return _AUTO_ANSWER
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        return False
    box = QMessageBox(parent)
    box.setWindowTitle("二次确认")
    box.setText(str(text))
    box.setIcon(QMessageBox.Warning)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.setDefaultButton(QMessageBox.No)
    return box.exec() == QMessageBox.Yes


# ---------------------------------------------------------------------------
# 1280 / 768 两档宽度适配
# ---------------------------------------------------------------------------

WIDTH_TIERS = {"wide": 1280, "narrow": 768}
WIDTH_CHOICES = (1280, 768)
# 窄档折叠次要列，只保留前 4 列（宽档全部显示）。
TIER_VISIBLE_COLUMNS = {"wide": None, "narrow": 4}


def width_tier(width: int) -> str:
    return "wide" if int(width) >= WIDTH_TIERS["wide"] else "narrow"


def _table_of(widget) -> QTableView | None:
    table = getattr(widget, "table_view", None)
    if isinstance(table, QTableView):
        return table
    if hasattr(widget, "findChild"):
        found = widget.findChild(QTableView)
        if isinstance(found, QTableView):
            return found
    return None


def apply_width(widget: QWidget, width: int) -> str:
    """把窗口适配到 1280 / 768 两档：窄档折叠表格列并保留横向滚动区。"""
    w = int(width)
    tier = width_tier(w)
    widget.setProperty("width_tier", tier)
    widget.setProperty("applied_width", w)
    widget.resize(w, max(widget.height(), 1))

    table = _table_of(widget)
    if table is not None:
        visible = TIER_VISIBLE_COLUMNS[tier]
        model = table.model()
        if model is not None:
            for col in range(model.columnCount()):
                table.setColumnHidden(col, visible is not None and col >= visible)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        header = table.horizontalHeader()
        if header is not None:
            header.setStretchLastSection(tier == "narrow")
    return tier
