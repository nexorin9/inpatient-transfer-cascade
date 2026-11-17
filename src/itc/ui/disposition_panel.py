"""逐条处置清单：业务字段 / 判定 / 证据 / 置信度 / 状态 + 逐条确认回写。

护士长的主工作面。每条处置项只有在被逐条确认后才会通过注入的 writer callable
发出一次写请求；writer 默认为 None 时只改本地状态，绝不直接连 HIS。
批量确认逐条独立，单条失败不影响其它条；失败项进入本地待重写队列并可重试。

停嘱草稿统一标注「草稿·需医师复核」，确认按钮 disabled，草稿正文只读可复制；
命中不可停品类的行标注「不可自动停嘱·需医师判断」且不产出草稿。
本模块与托盘菜单都不提供医嘱提交或签名入口。
"""
from __future__ import annotations

import json
from functools import partial
from typing import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from itc.ui import states, tokens

COLUMNS = ("业务对象", "判定", "执行科室", "证据字段", "证据片段", "置信度", "状态", "操作")
STATUS_COL = COLUMNS.index("状态")
ACTION_COL = COLUMNS.index("操作")

FILTERS = ("全部", "待确认", "已回写", "待人工")

_DEFAULT_FIELDS = {
    "transfer_no": "", "inpatient_no": "", "kind": "", "ref": "",
    "action": "", "judgement": "UNDETERMINED", "state": "awaiting_confirm",
    "confidence": 0.0, "evidence_fields": [], "evidence_snippet": "",
    "reasoning_summary": "", "stop_draft_content": "", "execute_dept_code": "",
    "execute_dept_name": "", "effective_from": "", "idempotency_key": "",
    "rule_hits": [], "manual_reason": "", "degraded": False,
    "degraded_reason": "", "confirmed_by": "", "role": "", "run_id": "", "id": None,
}


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return [value] if value else []
        return parsed if isinstance(parsed, list) else [parsed]
    return []


def _normalize(item) -> dict:
    if hasattr(item, "to_dict"):
        raw = item.to_dict()
    elif isinstance(item, dict):
        raw = dict(item)
    else:
        raw = dict(getattr(item, "__dict__", {}))
    out = dict(_DEFAULT_FIELDS)
    out.update({k: v for k, v in raw.items() if k in _DEFAULT_FIELDS})
    out["evidence_fields"] = _as_list(out["evidence_fields"])
    out["rule_hits"] = _as_list(out["rule_hits"])
    try:
        out["confidence"] = float(out["confidence"] or 0)
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    return out


class DispositionTableModel(QAbstractTableModel):
    """只读表格模型：每个单元格给一个展示字符串。"""

    def __init__(self, rows: list[dict], parent=None) -> None:
        super().__init__(parent)
        self._rows = rows
        self._columns = COLUMNS

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        # Qt.Horizontal 在新版 PySide6 是枚举对象；调用方传整数 1 时也要能取到列名
        if orientation == Qt.Horizontal or orientation == 1:  # 1 == Qt.Horizontal 的整数值
            return self._columns[section]
        return str(section + 1)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if role in (Qt.DisplayRole, Qt.EditRole):
            return self._rows[index.row()].get(self._columns[index.column()], "")
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None


class DispositionPanel(QWidget):
    """处置清单表 + 筛选条 + 批量操作 + 草稿只读区。"""

    itemConfirmed = Signal(object)
    itemFailed = Signal(object)
    exportRequested = Signal()
    clearRequested = Signal()

    def __init__(self, writer: Callable[[dict], object] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dispositionPanel")
        self._writer = writer
        self._items: list[dict] = []
        self._display_rows: list[dict] = []
        self._cell_widgets: list[QWidget] = []
        self.writer_calls: list[dict] = []
        self._confirmed_rows: set[int] = set()
        self._pending: dict[int, dict] = {}
        self._filter = "全部"
        self.last_copied_draft = ""
        self._build_ui()
        self.set_items([])

    # -- 界面搭建 -----------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(QLabel("筛选"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(FILTERS)
        self.filter_combo.currentTextChanged.connect(self.set_filter)
        bar.addWidget(self.filter_combo)
        bar.addStretch(1)
        self.confirm_all_button = QPushButton("批量确认")
        self.confirm_all_button.setProperty("variant", "secondary")
        self.confirm_all_button.clicked.connect(self._on_confirm_all_clicked)
        bar.addWidget(self.confirm_all_button)
        self.export_button = QPushButton("导出交接核对单")
        self.export_button.setProperty("variant", "secondary")
        self.export_button.clicked.connect(self._on_export_clicked)
        bar.addWidget(self.export_button)
        self.clear_button = QPushButton("清空清单")
        self.clear_button.setProperty("variant", "secondary")
        self.clear_button.clicked.connect(self._on_clear_clicked)
        bar.addWidget(self.clear_button)
        root.addLayout(bar)

        self._stack = QStackedWidget()
        self.table_view = QTableView()
        self.table_view.setObjectName("dispositionTable")
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSelectionBehavior(QTableView.SelectRows)
        self.table_view.setEditTriggers(QTableView.NoEditTriggers)
        self.table_view.verticalHeader().setDefaultSectionSize(36)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table_view.setStyleSheet(tokens.TABLE_QSS)
        self._stack.addWidget(self.table_view)

        self.empty_placeholder = states.StatePlaceholder()
        self.empty_placeholder.show_empty("list")
        self._stack.addWidget(self.empty_placeholder)
        root.addWidget(self._stack, 3)

        detail = QHBoxLayout()
        detail.setSpacing(12)
        left = QVBoxLayout()
        left.addWidget(QLabel("草稿正文（只读）"))
        self.draft_edit = QPlainTextEdit()
        self.draft_edit.setObjectName("draftEditor")
        self.draft_edit.setReadOnly(True)
        self.draft_edit.setMaximumBlockCount(200)
        self.draft_edit.setPlaceholderText("停嘱草稿仅供医师复核，本产品不代为确认")
        left.addWidget(self.draft_edit)
        self.copy_draft_button = QPushButton("复制草稿")
        self.copy_draft_button.setObjectName("copyDraftButton")
        self.copy_draft_button.setProperty("variant", "secondary")
        self.copy_draft_button.clicked.connect(self._on_copy_clicked)
        left.addWidget(self.copy_draft_button)
        detail.addLayout(left, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel("判定理由与引用证据"))
        self.evidence_edit = QPlainTextEdit()
        self.evidence_edit.setObjectName("evidenceEditor")
        self.evidence_edit.setReadOnly(True)
        self.evidence_edit.setMaximumBlockCount(200)
        right.addWidget(self.evidence_edit)
        detail.addLayout(right, 1)
        root.addLayout(detail, 1)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("runSummary")
        root.addWidget(self.summary_label)

        self._selected_draft_row: int | None = None

    # -- 数据装载 -----------------------------------------------------------
    def set_writer(self, writer: Callable[[dict], object] | None) -> None:
        self._writer = writer

    def set_items(self, items) -> None:
        self._items = [_normalize(x) for x in (items or [])]
        self._display_rows = [self._display_row(it) for it in self._items]
        self._confirmed_rows = {
            r for r, it in enumerate(self._items) if it["state"] == "written_back"
        }
        self._pending = {}
        self.writer_calls = []
        self.last_copied_draft = ""
        self._selected_draft_row = None
        self._rebuild_table()
        self._stack.setCurrentIndex(0 if self._items else 1)
        self.set_filter(self._filter)
        self._update_summary()
        for row in self.stop_draft_rows():
            self.show_draft(row)
            break

    def _display_row(self, item: dict) -> dict:
        evidence_fields = item["evidence_fields"]
        return {
            "业务对象": f"{item['kind'] or '-'} · {item['ref'] or '-'}",
            "判定": str(item["judgement"] or "-"),
            "执行科室": str(item["execute_dept_name"] or item["execute_dept_code"] or "-"),
            "证据字段": "、".join(str(x) for x in evidence_fields) or "-",
            "证据片段": str(item["evidence_snippet"] or "-"),
            "置信度": f"{item['confidence']:.2f}",
            "状态": self.status_text(item),
            "操作": "",
        }

    def _rebuild_table(self) -> None:
        model = DispositionTableModel(self._display_rows, self.table_view)
        self.table_view.setModel(model)
        self._cell_widgets = []
        for row in range(model.rowCount()):
            self._build_row_widgets(row)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

    def _build_row_widgets(self, row: int) -> None:
        model = self.table_view.model()
        item = self._items[row]

        label = QLabel(self.status_text(item))
        label.setObjectName(f"statusLabel_{row}")
        label.setAlignment(Qt.AlignCenter)
        self._apply_status_style(label, item)
        self.table_view.setIndexWidget(model.index(row, STATUS_COL), label)
        self._cell_widgets.append(label)

        widget = self._action_widget(row, item)
        self.table_view.setIndexWidget(model.index(row, ACTION_COL), widget)
        self._cell_widgets.append(widget)

    def _apply_status_style(self, label: QLabel, item: dict) -> None:
        if self._is_stop_draft(item):
            label.setStyleSheet(tokens.DRAFT_LABEL_QSS)
        elif self._is_no_stop(item):
            label.setStyleSheet(tokens.NO_STOP_LABEL_QSS)
        else:
            label.setStyleSheet(tokens.status_qss(tokens.resolve_status(item["state"], item)))

    def _action_widget(self, row: int, item: dict) -> QWidget:
        if self._is_no_stop(item):
            lab = QLabel("—")
            lab.setObjectName(f"actionLabel_{row}")
            lab.setAlignment(Qt.AlignCenter)
            return lab
        if item["state"] == "written_back":
            lab = QLabel("已回写")
            lab.setObjectName(f"actionLabel_{row}")
            lab.setAlignment(Qt.AlignCenter)
            return lab
        if item["state"] == "blocked":
            lab = QLabel("阻断")
            lab.setObjectName(f"actionLabel_{row}")
            lab.setAlignment(Qt.AlignCenter)
            return lab

        button = QPushButton()
        button.setObjectName(f"actionButton_{row}")
        self._configure_button(button, row, item)
        button.setProperty("variant", "secondary" if item["state"] == "failed" else "primary")
        return button

    def _configure_button(self, button: QPushButton, row: int, item: dict) -> None:
        # 用保存的连接句柄精确断开旧连接，避免同一按钮重复绑定多个行的回调
        old = getattr(button, "_click_conn", None)
        if old is not None:
            try:
                button.clicked.disconnect(old)
            except (RuntimeError, TypeError):
                pass
            button._click_conn = None
        if self._is_stop_draft(item):
            button.setText("确认回写")
            button.setEnabled(False)
            button.setToolTip("停嘱草稿需医师复核，本产品不代为确认")
            return
        if item["state"] == "failed":
            button.setText("重试")
            button.setEnabled(True)
            button._click_conn = button.clicked.connect(partial(self._on_retry_clicked, row))
            return
        button.setText("确认回写")
        button.setEnabled(True)
        button._click_conn = button.clicked.connect(partial(self._on_confirm_clicked, row))

    # -- 按钮点击适配器：QPushButton.clicked 会带一个 checked 布尔参数 -------
    def _on_confirm_clicked(self, row: int, _checked: bool = False) -> None:
        self.confirm_row(row)

    def _on_retry_clicked(self, row: int, _checked: bool = False) -> None:
        self.retry_failed(row)

    def _on_confirm_all_clicked(self, _checked: bool = False) -> dict:
        return self.confirm_all()

    def _on_export_clicked(self, _checked: bool = False) -> None:
        self.exportRequested.emit()

    def _on_clear_clicked(self, _checked: bool = False) -> bool:
        return self.clear_all()

    # -- 状态判定 -----------------------------------------------------------
    def _is_stop_draft(self, item: dict) -> bool:
        if self._is_no_stop(item):
            return False
        if str(item.get("judgement") or "") == "NEED_STOP_DRAFT":
            return True
        if str(item.get("action") or "").upper() == "NEED_STOP_DRAFT":
            return True
        return bool(str(item.get("stop_draft_content") or "").strip())

    def _is_no_stop(self, item: dict) -> bool:
        hits = [str(h) for h in item.get("rule_hits") or []]
        if any("no_stop" in h.lower() or "不可停" in h for h in hits):
            return True
        if "不可停" in str(item.get("manual_reason") or ""):
            return True
        if str(item.get("action") or "").upper() == "NO_STOP":
            return True
        return False

    def _confirmable(self, item: dict) -> bool:
        if self._is_stop_draft(item) or self._is_no_stop(item):
            return False
        return item["state"] not in ("written_back", "blocked", "failed")

    def status_text(self, item: dict) -> str:
        if self._is_no_stop(item):
            return tokens.NO_STOP_LABEL
        if self._is_stop_draft(item):
            return tokens.STOP_DRAFT_LABEL
        if item["state"] == "failed":
            return "失败·待重写"
        return tokens.status_label(item["state"], item)

    def status_text_for(self, row: int) -> str:
        return self.status_text(self._items[int(row)])

    def stop_draft_rows(self) -> list[int]:
        return [r for r, it in enumerate(self._items) if self._is_stop_draft(it)]

    def no_stop_rows(self) -> list[int]:
        return [r for r, it in enumerate(self._items) if self._is_no_stop(it)]

    def failed_rows(self) -> list[int]:
        return [r for r, it in enumerate(self._items) if it["state"] == "failed"]

    # -- 写请求（注入 writer） ----------------------------------------------
    @staticmethod
    def _write_payload(item: dict) -> dict:
        return {
            "transfer_no": str(item.get("transfer_no") or ""),
            "inpatient_no": str(item.get("inpatient_no") or ""),
            "kind": str(item.get("kind") or ""),
            "ref": str(item.get("ref") or ""),
            "action": str(item.get("action") or ""),
            "judgement": str(item.get("judgement") or ""),
            "execute_dept_code": str(item.get("execute_dept_code") or ""),
            "execute_dept_name": str(item.get("execute_dept_name") or ""),
            "effective_from": str(item.get("effective_from") or ""),
            "idempotency_key": str(item.get("idempotency_key") or ""),
            "evidence_fields": list(item.get("evidence_fields") or []),
            "evidence_snippet": str(item.get("evidence_snippet") or ""),
            "confidence": float(item.get("confidence") or 0),
        }

    def _call_writer(self, payload: dict) -> tuple[bool, str]:
        """无 writer 时只改本地状态，不发任何写请求。"""
        if self._writer is None:
            return True, ""
        self.writer_calls.append(dict(payload))
        try:
            result = self._writer(dict(payload))
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if isinstance(result, dict):
            ok = result.get("ok")
            status = str(result.get("status") or "").lower()
            if ok is False or status in ("failed", "error", "rejected"):
                return False, str(result.get("reason") or "写回失败")
        return True, ""

    # -- 逐条确认 / 批量 / 重试 ---------------------------------------------
    def confirm_row(self, row, actor: str = "护士长") -> bool:
        """确认单行：只有确认后才调用一次 writer；停嘱草稿与不可停品类直接拒绝。"""
        r = int(row)
        if not (0 <= r < len(self._items)) or not self._confirmable(self._items[r]):
            return False
        item = self._items[r]
        ok, reason = self._call_writer(self._write_payload(item))
        self._apply_result(r, ok, reason, actor)
        return ok

    def _apply_result(self, row: int, ok: bool, reason: str, actor: str) -> None:
        item = self._items[row]
        if ok:
            item["state"] = "written_back"
            item["confirmed_by"] = actor
            item["role"] = actor
            self._confirmed_rows.add(row)
            self._pending.pop(row, None)
            self.itemConfirmed.emit(dict(item))
        else:
            item["state"] = "failed"
            item["manual_reason"] = reason or "写回失败"
            entry = self._pending.setdefault(row, {
                "row": row,
                "reason": reason or "写回失败",
                "attempts": 0,
                "idempotency_key": str(item.get("idempotency_key") or ""),
                "ref": str(item.get("ref") or ""),
            })
            entry["attempts"] += 1
            entry["reason"] = reason or "写回失败"
            self.itemFailed.emit(dict(item))
        self._refresh_row(row)
        self._update_summary()

    def confirm_all(self, actor: str = "护士长") -> dict:
        """批量确认逐条独立：单条失败被就地记录，不影响其它条继续确认。"""
        attempted = 0
        succeeded = 0
        failed: list[int] = []
        for row in [r for r in range(len(self._items)) if self._confirmable(self._items[r])]:
            attempted += 1
            try:
                ok = self.confirm_row(row, actor=actor)
            except Exception as exc:  # 兜底：任何意外都不阻断后续条目
                ok = False
                self._apply_result(row, False, f"{type(exc).__name__}: {exc}", actor)
            if ok:
                succeeded += 1
            else:
                failed.append(row)
        return {"attempted": attempted, "succeeded": succeeded, "failed": failed}

    def retry_failed(self, row) -> bool:
        """失败项的重试入口：保留同一幂等键，再次调用一次 writer。"""
        r = int(row)
        if not (0 <= r < len(self._items)) or self._items[r]["state"] != "failed":
            return False
        item = self._items[r]
        ok, reason = self._call_writer(self._write_payload(item))
        self._apply_result(r, ok, reason, item.get("confirmed_by") or "护士长")
        return ok

    def _refresh_row(self, row: int) -> None:
        """原地刷新状态标签与操作按钮，不销毁正在发射信号的按钮。"""
        model = self.table_view.model()
        if model is None:
            return
        item = self._items[row]
        self._display_rows[row]["状态"] = self.status_text(item)
        label = self.table_view.indexWidget(model.index(row, STATUS_COL))
        if isinstance(label, QLabel):
            label.setText(self.status_text(item))
            self._apply_status_style(label, item)
        action = self.table_view.indexWidget(model.index(row, ACTION_COL))
        if isinstance(action, QPushButton):
            self._configure_button(action, row, item)
        elif isinstance(action, QLabel):
            action.setText("已回写" if item["state"] == "written_back" else "—")
        model.dataChanged.emit(model.index(row, 0), model.index(row, len(COLUMNS) - 1))

    def _update_summary(self) -> None:
        total = len(self._items)
        confirmed = len([it for it in self._items if it["state"] == "written_back"])
        awaiting = len([it for it in self._items if tokens.resolve_status(it["state"], it) == "awaiting"])
        manual = len([it for it in self._items if tokens.resolve_status(it["state"], it) == "manual"])
        failed = len(self.failed_rows())
        self.summary_label.setText(
            f"清单 {total} 条 · 已回写 {confirmed} · 待确认 {awaiting} · "
            f"待人工 {manual} · 待重写 {failed}"
        )

    def summary_text(self) -> str:
        return self.summary_label.text()

    def write_request_count(self) -> int:
        return len(self.writer_calls)

    def confirmed_items(self) -> list[dict]:
        return [dict(it) for it in self._items if it["state"] == "written_back"]

    def pending_writebacks(self) -> list[dict]:
        return [dict(v) for v in sorted(self._pending.values(), key=lambda x: x["row"])]

    def retry_entries(self) -> list[dict]:
        return self.pending_writebacks()

    # -- 停嘱草稿 -----------------------------------------------------------
    def draft_text_for(self, row) -> str:
        r = int(row)
        if not (0 <= r < len(self._items)) or self._is_no_stop(self._items[r]):
            return ""
        return str(self._items[r].get("stop_draft_content") or "")

    def show_draft(self, row) -> str:
        """把草稿正文与判定理由 / 证据同屏显示在两个只读文本框中。"""
        r = int(row)
        if not (0 <= r < len(self._items)):
            self.draft_edit.setPlainText("")
            self.evidence_edit.setPlainText("")
            return ""
        item = self._items[r]
        self._selected_draft_row = r
        draft = self.draft_text_for(r)
        self.draft_edit.setPlainText(draft)
        fields = "、".join(str(x) for x in item["evidence_fields"]) or "-"
        self.evidence_edit.setPlainText(
            "判定：" + str(item["judgement"] or "-")
            + "\n理由：" + str(item["reasoning_summary"] or "-")
            + "\n引用字段：" + fields
            + "\n原文片段：" + str(item["evidence_snippet"] or "-")
        )
        return draft

    def copy_draft(self, row) -> str:
        """草稿正文可复制：写入系统剪贴板，同时留一份可断言的本地副本。"""
        text = self.draft_text_for(row)
        self.last_copied_draft = text
        app = QApplication.instance()
        if app is not None and hasattr(app, "clipboard"):
            try:
                app.clipboard().setText(text)
            except RuntimeError:
                pass
        return text

    def _on_copy_clicked(self, _checked: bool = False) -> None:
        row = self._selected_draft_row
        if row is None:
            rows = self.stop_draft_rows()
            row = rows[0] if rows else None
        if row is not None:
            self.show_draft(row)
            self.copy_draft(row)

    # -- 筛选与破坏性操作 ---------------------------------------------------
    def set_filter(self, key: str) -> list[int]:
        key = str(key)
        self._filter = key if key in FILTERS else "全部"
        if not hasattr(self, "table_view"):
            return []
        for row, item in enumerate(self._items):
            self.table_view.setRowHidden(row, not self._match_filter(item))
        return self.visible_rows()

    def _match_filter(self, item: dict) -> bool:
        if self._filter == "全部":
            return True
        if self._filter == "已回写":
            return item["state"] == "written_back"
        if self._filter == "待人工":
            return (tokens.resolve_status(item["state"], item) == "manual"
                    or self._is_no_stop(item) or self._is_stop_draft(item))
        return tokens.resolve_status(item["state"], item) == "awaiting"

    def visible_rows(self) -> list[int]:
        return [r for r in range(len(self._items)) if not self.table_view.isRowHidden(r)]

    def clear_all(self, prompt: str = "确认清空当前处置清单？未确认项将不再保留。") -> bool:
        """破坏性操作：先二次确认，再清空。"""
        if not states.confirm_destructive(self, prompt):
            return False
        self.set_items([])
        self.clearRequested.emit()
        return True
