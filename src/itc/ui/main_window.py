"""桌面主窗口：中后台导航 + 页头区 + 内容区。

承载两个入口：把转科单拖进窗口（或粘贴 JSON 文本）、从 HIS 拉取今日转科。
导入 / 入队后按模板建图，并消费 itc.graph.events.ProgressEvent 显示节点进度。
字段不合规时给行级提示；加载 / 空 / 错误三态由 ui.states 提供。

本模块只依赖底座、G2 的 itc.graph.events、S 组 ledger（经 ui.handover），
不 import H / M 组模块；今日转科列表由外部注入的 today_loader 提供，
UI 自身绝不直接发起 HIS 写请求，也不提供医嘱提交或签名入口。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from itc.domain.models import FieldMissingError, TransferOrder
from itc.graph.events import ProgressEvent
from itc.ui import handover, states, tokens, tray
from itc.ui.disposition_panel import DispositionPanel
from itc.ui.graph_view import GraphView

NAV_ITEMS = ("转科处置", "待确认清单", "待重写队列", "交接核对单")

# 入队模板：视觉目标卡 §1.2 的五卡流程，只作进度可视化。
DEFAULT_GRAPH_NODES = (
    {"id": "transfer", "class_type": "transfer", "title": "转科单"},
    {"id": "fetch", "class_type": "fetch_orders", "title": "取数"},
    {"id": "rule", "class_type": "rule_judge", "title": "规则判定"},
    {"id": "model", "class_type": "model_judge", "title": "模型判定"},
    {"id": "confirm", "class_type": "confirm_writeback", "title": "确认回写"},
)


def build_transfer_graph(transfer_no: str = "") -> dict:
    """按模板建一张轻量图（不 import G1 图模型，避免越界依赖）。"""
    nodes = [dict(n) for n in DEFAULT_GRAPH_NODES]
    if transfer_no:
        nodes[0]["title"] = f"转科单 {transfer_no}"
    links = [
        {"from": nodes[i]["id"], "to": nodes[i + 1]["id"]}
        for i in range(len(nodes) - 1)
    ]
    return {"transfer_no": transfer_no, "nodes": nodes, "links": links}


class MainWindow(QMainWindow):
    """主窗口骨架 + 导入 / 拉取 / 建图 / 进度 / 三态 / toast / 二次确认。"""

    nodeSelected = Signal(str)
    transfersLoaded = Signal(int)

    def __init__(self, conn=None, qapp=None, bus=None) -> None:
        super().__init__()
        self.setObjectName("mainWindow")
        self.setWindowTitle("住院转科连带处置")
        self.setAcceptDrops(True)

        self.conn = conn
        self.qapp = qapp or QApplication.instance()
        self.bus = bus
        self.today_loader: Callable[[], object] | None = None
        self.writer: Callable[[dict], object] | None = None
        self.on_export: Callable[[Path], None] | None = None
        self.on_node_clicked: Callable[[str], None] | None = None
        self.tray: tray.TrayController | None = None

        self._state = "empty"
        self._transfer_no = ""
        self._items: list = []
        self._today_rows: list[dict] = []
        self.last_import_errors: list[str] = []
        self.last_import_result: dict = {}
        self.last_drop_result: dict = {}
        self.last_clicked_node = ""
        self.restore_count = 0
        self.quit_accepted = False

        self._build_ui()
        tokens.apply_theme(self)
        self._wire_signals()
        if self.bus is not None:
            self.subscribe_bus(self.bus)
        self.show_state("empty")
        self.ensure_tray()

    # -- 界面骨架 -----------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("contentArea")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左侧固定导航（200px，浅灰底）
        nav = QWidget()
        nav.setObjectName("navPanel")
        nav.setFixedWidth(tokens.LAYOUT["nav_width"])
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(12, 24, 12, 24)
        nav_layout.setSpacing(8)
        self.nav_buttons: dict[str, QPushButton] = {}
        for text in NAV_ITEMS:
            button = QPushButton(text)
            button.setObjectName(f"nav_{text}")
            button.setProperty("variant", "secondary")
            button.setCheckable(True)
            button.clicked.connect(self._on_nav_clicked)
            nav_layout.addWidget(button)
            self.nav_buttons[text] = button
        nav_layout.addStretch(1)
        root.addWidget(nav)

        # 右侧：页头区 + 内容区 + 底部状态摘要
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(24, 24, 24, 12)
        right_layout.setSpacing(16)

        header = QWidget()
        header.setObjectName("headerBar")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        titles = QVBoxLayout()
        title = QLabel("住院转科连带处置")
        title.setObjectName("pageTitle")
        desc = QLabel("HIS 下完转科单后，把转科单拖进来，当场算出牵动范围")
        desc.setObjectName("helperText")
        titles.addWidget(title)
        titles.addWidget(desc)
        header_layout.addLayout(titles)
        header_layout.addStretch(1)
        self.pull_today_button = QPushButton("拉取今日转科")
        self.pull_today_button.setObjectName("pullTodayButton")
        header_layout.addWidget(self.pull_today_button)
        right_layout.addWidget(header)

        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("contentStack")
        self.content_stack.addWidget(self._build_empty_page())     # 0：三态
        self.content_stack.addWidget(self._build_workspace_page())  # 1：图 + 清单
        right_layout.addWidget(self.content_stack, 1)

        self.status_summary = QLabel("HIS 未连通 · 模型未连通")
        self.status_summary.setObjectName("runSummary")
        right_layout.addWidget(self.status_summary)
        root.addWidget(right, 1)

    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.placeholder = states.StatePlaceholder()
        layout.addWidget(self.placeholder, 1)

        strip = QWidget()
        strip.setObjectName("cardPanel")
        strip_layout = QVBoxLayout(strip)
        strip_layout.setContentsMargins(16, 16, 16, 16)
        strip_layout.setSpacing(8)
        hint = QLabel("可将转科单 .json 文件直接拖入本窗口，或粘贴 JSON 文本")
        hint.setObjectName("helperText")
        strip_layout.addWidget(hint)
        self.paste_edit = QPlainTextEdit()
        self.paste_edit.setObjectName("pasteEdit")
        self.paste_edit.setPlaceholderText('{"transfer_no": "...", "inpatient_no": "...", ...}')
        self.paste_edit.setFixedHeight(72)
        strip_layout.addWidget(self.paste_edit)
        self.import_errors_label = QLabel("")
        self.import_errors_label.setObjectName("importErrors")
        self.import_errors_label.setWordWrap(True)
        self.import_errors_label.setStyleSheet(tokens.DANGER_TEXT_QSS)
        strip_layout.addWidget(self.import_errors_label)
        row = QHBoxLayout()
        row.addStretch(1)
        self.paste_button = QPushButton("粘贴 JSON 文本")
        self.paste_button.setObjectName("pasteJsonButton")
        self.paste_button.setProperty("variant", "secondary")
        row.addWidget(self.paste_button)
        strip_layout.addLayout(row)
        layout.addWidget(strip)
        return page

    def _build_workspace_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        section = QLabel("联动进度")
        section.setObjectName("sectionTitle")
        layout.addWidget(section)

        splitter = QSplitter(Qt.Vertical)
        self.graph_view = GraphView()
        self.disposition_panel = DispositionPanel(writer=self.writer)
        splitter.addWidget(self.graph_view)
        splitter.addWidget(self.disposition_panel)
        splitter.setStretchFactor(0, 4)  # 图不超过内容区约 45%
        splitter.setStretchFactor(1, 6)
        layout.addWidget(splitter, 1)

        self.run_summary = QLabel("取数 0 类 · 判定 0 条 · 阻断 0 条")
        self.run_summary.setObjectName("runSummary")
        layout.addWidget(self.run_summary)
        return page

    def _wire_signals(self) -> None:
        self.pull_today_button.clicked.connect(self._on_pull_today)
        self.paste_button.clicked.connect(self._on_paste_clicked)
        self.graph_view.set_node_clicked_callback(self._on_node_clicked)
        self.disposition_panel.exportRequested.connect(self._on_export_clicked)
        self.disposition_panel.clearRequested.connect(
            lambda: states.show_toast(self, "处置清单已清空")
        )
        self.disposition_panel.itemConfirmed.connect(self._on_item_confirmed)
        self.disposition_panel.itemFailed.connect(self._on_item_failed)
        self.placeholder.retry_callback = self._on_retry
        self._select_nav("转科处置")

    # -- 导航与页头 ---------------------------------------------------------
    def _select_nav(self, text: str) -> None:
        for key, button in self.nav_buttons.items():
            button.setChecked(key == text)

    def _on_nav_clicked(self, _checked: bool = False) -> None:
        sender = self.sender()
        if not isinstance(sender, QPushButton):
            return
        text = sender.text()
        self._select_nav(text)
        if text == "待确认清单":
            self.disposition_panel.set_filter("待确认")
        elif text == "待重写队列":
            self.disposition_panel.set_filter("待人工")
        elif text == "交接核对单":
            self._on_export_clicked()
        else:
            self.disposition_panel.set_filter("全部")

    def _on_pull_today(self, _checked: bool = False) -> dict:
        return self.request_today_transfers()

    def _on_paste_clicked(self, _checked: bool = False) -> dict:
        return self.import_transfer(self.paste_edit.toPlainText())

    def paste_json(self, text: str) -> dict:
        return self.import_transfer(text)

    # -- 三态 ---------------------------------------------------------------
    def show_state(self, state, message: str = "") -> str:
        """切换加载 / 空 / 错误 / 工作区四页（三态各有独立渲染）。"""
        if isinstance(state, dict):
            message = state.get("message", message)
            state = state.get("state", "empty")
        key = str(state)
        if key in ("ready", "workspace", "loaded"):
            self.content_stack.setCurrentIndex(1)
            self._state = "ready"
            return self._state
        if key == "loading":
            self.placeholder.show_loading(message or "fetch")
        elif key == "error":
            self.placeholder.show_error(message or "his")
        else:
            self.placeholder.show_empty(message or "today")
        self.content_stack.setCurrentIndex(0)
        self._state = self.placeholder.state()
        return self._state

    def current_state(self) -> str:
        return self._state

    def validation_errors(self) -> list[str]:
        """最近一次导入 / 拉取的行级字段校验提示。"""
        return list(self.last_import_errors)

    def _on_retry(self) -> None:
        self.show_state("loading", "fetch")
        states.show_toast(self, "正在重新取数")

    def set_run_summary(self, text: str) -> None:
        self.run_summary.setText(str(text))

    def run_summary_text(self) -> str:
        return self.run_summary.text()

    def set_status_summary(self, text: str) -> None:
        self.status_summary.setText(str(text))

    # -- 拉取今日转科（外部注入 loader，UI 不 import H 组） ------------------
    def request_today_transfers(self) -> dict:
        if not callable(self.today_loader):
            states.show_toast(self, "HIS 未连通，请拖入转科单 JSON 或粘贴 JSON 文本")
            self.set_status_summary("HIS 未连通 · 模型未连通")
            return {"ok": False, "reason": "his_unreachable", "count": 0}
        self.show_state("loading", "fetch")
        try:
            rows = self.today_loader()
        except Exception as exc:
            self.show_state("error", "his")
            states.show_toast(self, f"拉取今日转科失败：{type(exc).__name__}")
            return {"ok": False, "reason": str(exc), "count": 0}
        return self.load_today_transfers(rows)

    def load_today_transfers(self, rows) -> dict:
        """装载今日转科列表：逐行校验，行级提示，选中首条入队建图。"""
        normalized = self._to_row_list(rows)
        self._today_rows = []
        errors: list[str] = []
        for index, row in enumerate(normalized, start=1):
            try:
                self._today_rows.append(TransferOrder.from_dict(row).to_dict())
            except (FieldMissingError, ValueError, TypeError) as exc:
                errors.append(f"第 {index} 行：{exc}")
        self.last_import_errors = errors
        self.import_errors_label.setText("\n".join(errors))
        self.transfersLoaded.emit(len(self._today_rows))
        if errors:
            states.show_toast(self, f"今日转科列表有 {len(errors)} 行字段不合规")
        if not self._today_rows:
            self.show_state("empty", "today")
            return {"ok": not errors, "count": 0, "errors": errors}
        self.status_summary.setText(f"今日转科 {len(self._today_rows)} 张 · HIS 已返回列表")
        states.show_toast(self, f"已拉取今日转科 {len(self._today_rows)} 张")
        return self._enqueue(self._today_rows[0])

    def select_today_transfer(self, index: int) -> dict:
        if not (0 <= int(index) < len(self._today_rows)):
            return {"ok": False, "reason": "index_out_of_range"}
        return self._enqueue(self._today_rows[int(index)])

    # -- 导入 / 拖入 / 粘贴 -------------------------------------------------
    @staticmethod
    def _parse_source(source) -> tuple[object | None, str]:
        """把路径 / JSON 文本 / dict / list 统一解析成 Python 数据。"""
        if isinstance(source, (dict, list)):
            return source, ""
        if isinstance(source, Path):
            text = source.read_text(encoding="utf-8")
        elif isinstance(source, str):
            candidate = source.strip()
            if candidate and os.path.isfile(candidate):
                text = Path(candidate).read_text(encoding="utf-8")
            else:
                text = candidate
        else:
            return None, "不支持的导入源"
        if not text.strip():
            return None, "导入内容为空"
        try:
            return json.loads(text), ""
        except (ValueError, TypeError) as exc:
            return None, f"JSON 解析失败：{exc}"

    @staticmethod
    def _to_row_list(data) -> list[dict]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("items", "transfers", "transfers_today", "rows"):
                if isinstance(data.get(key), list):
                    return [x for x in data[key] if isinstance(x, dict)]
            return [data]
        return []

    def import_transfer(self, path_or_text) -> dict:
        """导入转科单：字段不合规给行级提示，合规则入队即建图并显示节点进度。"""
        data, parse_error = self._parse_source(path_or_text)
        if parse_error:
            self.last_import_errors = [parse_error]
            self.import_errors_label.setText(parse_error)
            self.paste_edit.setProperty("invalid", "true")
            states.show_toast(self, parse_error)
            self.last_import_result = {"ok": False, "errors": [parse_error], "node_count": 0}
            return self.last_import_result

        rows = self._to_row_list(data)
        errors: list[str] = []
        valid: list[dict] = []
        for index, row in enumerate(rows, start=1):
            try:
                valid.append(TransferOrder.from_dict(row).to_dict())
            except (FieldMissingError, ValueError, TypeError) as exc:
                errors.append(f"第 {index} 行：{exc}")
        self.last_import_errors = errors
        self.import_errors_label.setText("\n".join(errors))
        self.paste_edit.setProperty("invalid", "true" if errors else "false")
        self.paste_edit.style().polish(self.paste_edit)
        if errors:
            states.show_toast(self, f"有 {len(errors)} 行字段不合规，请按行级提示修正")
            self.last_import_result = {"ok": False, "errors": errors, "node_count": 0}
            return self.last_import_result
        if not valid:
            errors = ["第 1 行：未解析到任何转科单"]
            self.last_import_errors = errors
            self.import_errors_label.setText(errors[0])
            self.last_import_result = {"ok": False, "errors": errors, "node_count": 0}
            return self.last_import_result
        return self._enqueue(valid[0])

    # -- 入队即建图 + 进度事件 ----------------------------------------------
    def _enqueue(self, transfer: dict) -> dict:
        self._transfer_no = str(transfer.get("transfer_no") or "")
        graph = build_transfer_graph(self._transfer_no)
        self.show_state("ready")
        self.graph_view.set_graph(graph)
        self.set_run_summary(
            f"转科单 {self._transfer_no} 已入队 · 节点 {len(graph['nodes'])} 个 · 取数待执行"
        )
        # 入队完成：首个节点着色，取数节点进入执行中（消费进度事件）
        start = ProgressEvent(
            run_id=self._transfer_no or "ui", node_id="transfer",
            node_type="transfer", status="success", message="转科单已入队",
        )
        running = ProgressEvent(
            run_id=self._transfer_no or "ui", node_id="fetch",
            node_type="fetch_orders", status="running", message="正在取数",
        )
        if self.bus is not None:
            self.bus.emit(start)
            self.bus.emit(running)
        else:
            self.on_progress(start)
            self.on_progress(running)
        states.show_toast(self, f"转科单 {self._transfer_no} 已入队并开始建图")
        result = {
            "ok": True, "errors": [], "transfer_no": self._transfer_no,
            "node_count": len(graph["nodes"]), "graph": graph,
        }
        self.last_import_result = result
        return result

    def on_progress(self, event) -> None:
        """消费 itc.graph.events.ProgressEvent，更新联动图节点状态。"""
        self.graph_view.update_progress(event)
        node_id = event.node_id if isinstance(event, ProgressEvent) else (
            event.get("node_id") if isinstance(event, dict) else getattr(event, "node_id", ""))
        status = event.status if isinstance(event, ProgressEvent) else (
            event.get("status") if isinstance(event, dict) else getattr(event, "status", ""))
        self.set_run_summary(
            f"转科单 {self._transfer_no} · 节点 {node_id} {status} · 进度已更新"
        )

    def subscribe_bus(self, bus) -> None:
        self.bus = bus
        bus.subscribe(self.on_progress)

    # -- 处置清单 / 交接单 / 托盘 -------------------------------------------
    def set_disposition_items(self, items) -> None:
        self._items = list(items or [])
        self.disposition_panel.set_writer(self.writer)
        self.disposition_panel.set_items(self._items)
        self.show_state("ready")
        self._sync_tray_counts()

    def set_writer(self, writer) -> None:
        self.writer = writer
        self.disposition_panel.set_writer(writer)

    def _on_item_confirmed(self, _item) -> None:
        self._sync_tray_counts()
        states.show_toast(self, "已确认一条，回写完成，可导出交接核对单交给病区")

    def _on_item_failed(self, _item) -> None:
        self._sync_tray_counts()
        states.show_toast(self, "写回失败，已转入待重写队列，可保留同一幂等键重试")

    @staticmethod
    def _item_state(it) -> str:
        return str(it.get("state", "") if isinstance(it, dict) else getattr(it, "state", ""))

    def _sync_tray_counts(self) -> None:
        if self.tray is None:
            return
        open_states = ("pending_fetch", "scoped", "rule_judged",
                       "model_judged", "awaiting_confirm")
        awaiting = len([it for it in self._items if self._item_state(it) in open_states])
        retry = len(self.disposition_panel.pending_writebacks())
        self.tray.set_counts(awaiting=awaiting, retry=retry)

    def default_handover_path(self) -> Path:
        name = f"handover_{self._transfer_no or 'current'}.csv"
        return Path("outputs") / name

    def export_handover(self, out_path=None) -> Path:
        """导出脱敏交接核对单：有 conn 走台账，否则用当前清单 items。"""
        path = Path(out_path) if out_path is not None else self.default_handover_path()
        items = None if self.conn is not None else (self._items or None)
        transfer_no = self._transfer_no or handover.first_transfer_no(self.conn)
        result = handover.export_handover(self.conn, transfer_no, path, items=items)
        states.show_toast(self, "交接核对单已导出")
        if callable(self.on_export):
            self.on_export(result)
        return result

    def _on_export_clicked(self) -> Path | None:
        try:
            return self.export_handover()
        except (ValueError, OSError) as exc:
            self.show_state("error", "writeback")
            states.show_toast(self, f"导出失败：{exc}")
            return None

    def _on_node_clicked(self, node_id: str) -> None:
        self.last_clicked_node = str(node_id)
        self.disposition_panel.set_filter("全部")
        self.nodeSelected.emit(str(node_id))
        if callable(self.on_node_clicked):
            self.on_node_clicked(str(node_id))

    def restore_window(self) -> None:
        self.restore_count += 1
        self.show()
        # 无头平台不支持窗口提升 / 激活，跳过以免产生平台告警
        if os.environ.get("QT_QPA_PLATFORM", "").lower() != "offscreen":
            self.raise_()
            self.activateWindow()

    def request_quit(self) -> bool:
        """退出前对未确认项做破坏性操作二次确认。"""
        pending = len([it for it in self._items
                       if self._item_state(it) not in ("written_back", "blocked")])
        prompt = "仍有未确认的处置项，确认退出？" if pending else "确认退出应用？"
        self.quit_accepted = states.confirm_destructive(self, prompt)
        if self.quit_accepted:
            self.close()
        return self.quit_accepted

    def ensure_tray(self) -> tray.TrayController:
        if self.tray is None:
            self.tray = tray.TrayController(self)
            self.tray.on_restore = self.restore_window
            self.tray.set_action_handler("拉取今日转科", self.request_today_transfers)
            self.tray.set_action_handler(
                "待确认清单", lambda: self.disposition_panel.set_filter("待确认"))
            self.tray.set_action_handler(
                "待重写队列", lambda: self.disposition_panel.set_filter("待人工"))
            self.tray.set_action_handler("退出", self.request_quit)
            self._sync_tray_counts()
        return self.tray

    # -- 宽度适配 -----------------------------------------------------------
    def set_width(self, width: int) -> str:
        return states.apply_width(self, width)

    # -- 拖放 ---------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if mime is not None and (mime.hasUrls() or mime.hasText()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        result: dict = {"ok": False, "reason": "no_json_payload"}
        if mime is not None and mime.hasUrls():
            for url in mime.urls():
                local = url.toLocalFile()
                if local.lower().endswith(".json") and os.path.isfile(local):
                    result = self.import_transfer(local)
                    break
        elif mime is not None and mime.hasText():
            result = self.import_transfer(mime.text())
        self.last_drop_result = result
        event.acceptProposedAction()
