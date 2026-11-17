"""联动图视图：QGraphicsView 画节点卡 + 带箭头连线 + 阻断标记 + 进度着色。

图只作进度可视化，不承载数据录入。节点状态取自 itc.graph.events 的进度事件；
点击节点通过回调把选中节点交给逐条处置清单。

只依赖 PySide6 2D 控件、ui.tokens 与 G2 的 itc.graph.events（按契约 import），
不接触 HIS / 模型服务，也不提供医嘱提交与签名路径。
"""
from __future__ import annotations

import math
from typing import Callable, Iterable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView

from itc.graph.events import NODE_STATUS, ProgressEvent
from itc.ui import tokens

# 节点类型 -> 中文标题（与视觉目标卡 §1.2 的五卡流程一致）。
# 不 import G1 registry，避免越界依赖；未知类型回退到类型名本身。
NODE_TYPE_LABELS = {
    "fetch_orders": "取数", "fetch_specimens": "取数", "fetch_charges": "取数",
    "rule_judge": "规则判定", "model_judge": "模型判定",
    "change_site": "改执行地点", "specimen_route": "送标本去向",
    "bed_fee_hint": "床位费归属", "stop_draft": "停嘱草稿",
    "external_block": "外部阻断", "confirm_writeback": "确认回写",
    "transfer": "转科单", "confirm": "确认回写",
}

# 中文状态标签 / 处置状态 -> 节点状态灯 key
_STATUS_ALIASES = {
    "待执行": "pending", "执行中": "running", "已完成": "success",
    "待人工": "manual", "待人工判定": "manual", "阻断": "blocked",
    "失败": "failed", "已回写": "success", "待确认": "manual",
    "awaiting_confirm": "manual", "written_back": "success",
    "model_judged": "manual",
}

CARD_W = 140
CARD_H = 68
STEP_X = 200
MARGIN = 24


def _norm_status(status: str) -> str:
    raw = str(status or "pending")
    if raw in tokens.NODE_STATUS_COLORS:
        return raw
    if raw in _STATUS_ALIASES:
        return _STATUS_ALIASES[raw]
    if raw in NODE_STATUS:
        return raw
    return "pending"


def _node_from(node, default_id: str) -> dict:
    if isinstance(node, dict):
        nid = str(node.get("id") or node.get("node_id") or default_id)
        class_type = str(node.get("class_type") or node.get("type") or "unknown")
        title = str(node.get("title") or node.get("display_name")
                    or NODE_TYPE_LABELS.get(class_type, class_type))
        return {
            "id": nid, "class_type": class_type, "title": title,
            "status": _norm_status(node.get("status")),
        }
    nid = str(getattr(node, "id", default_id))
    class_type = str(getattr(node, "class_type", "unknown"))
    return {
        "id": nid,
        "class_type": class_type,
        "title": NODE_TYPE_LABELS.get(class_type, class_type),
        "status": "pending",
    }


def _link_from(link) -> dict:
    if isinstance(link, dict):
        src = link.get("from") or link.get("from_node_id") or link.get("source") or ""
        dst = link.get("to") or link.get("to_node_id") or link.get("target") or ""
    else:
        src = getattr(link, "from_node_id", "")
        dst = getattr(link, "to_node_id", "")
    return {"from": str(src), "to": str(dst)}


def normalize_graph(graph) -> tuple[list[dict], list[dict]]:
    """把 G1 Graph 对象 / to_dict() 字典 / 节点字典列表统一成 (nodes, links)。

    支持三类输入：
      1. itc.graph.model.Graph（nodes 为 dict 或 list，links 为 Link 对象列表）
      2. Graph.to_dict() 形态（nodes 为 {id: {class_type, inputs}}）
      3. 主窗口自建的轻量图（nodes 为带 id/title 的 dict 列表，links 带 from/to）
    """
    nodes: list[dict] = []
    links: list[dict] = []

    if graph is None:
        return nodes, links

    if hasattr(graph, "nodes") and hasattr(graph, "links"):
        raw_nodes = graph.nodes
        if isinstance(raw_nodes, dict):
            for nid, node in raw_nodes.items():
                nodes.append(_node_from(node, str(nid)))
        else:
            for i, node in enumerate(raw_nodes):
                nodes.append(_node_from(node, str(getattr(node, "id", i))))
        for link in graph.links:
            links.append(_link_from(link))
        return nodes, links

    if isinstance(graph, dict):
        raw_nodes = graph.get("nodes", [])
        if isinstance(raw_nodes, dict):
            for nid, node in raw_nodes.items():
                nodes.append(_node_from(node, str(nid)))
            # to_dict() 未显式给 links 时，从 inputs 的连线值推导
            if not graph.get("links"):
                for nid, node in raw_nodes.items():
                    inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
                    for value in inputs.values():
                        if (isinstance(value, list) and len(value) == 2
                                and isinstance(value[0], str)):
                            links.append({"from": value[0], "to": str(nid)})
        else:
            for i, node in enumerate(raw_nodes):
                nodes.append(_node_from(node, str(i)))
        for link in graph.get("links", []) or []:
            links.append(_link_from(link))
        return nodes, links

    if isinstance(graph, Iterable):
        for i, node in enumerate(graph):
            nodes.append(_node_from(node, str(i)))
    return nodes, links


class NodeCardItem(QGraphicsItem):
    """节点卡：类型 / 标题 / 状态色圆点 / 阻断或失败标记，全部由 QPainter 绘制。"""

    def __init__(self, spec: dict, view: "GraphView") -> None:
        super().__init__()
        self.node_id = spec["id"]
        self.class_type = spec["class_type"]
        self.title = spec["title"]
        self.status = spec["status"]
        self._view = view
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsFocusable, True)
        self.setAcceptHoverEvents(True)
        self.setToolTip(f"{self.class_type} · {self.title}")

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, CARD_W, CARD_H)

    def set_status(self, status: str) -> None:
        self.status = _norm_status(status)
        self.update()

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.boundingRect()
        color = QColor(tokens.node_status_color(self.status))

        painter.setPen(QPen(QColor(tokens.COLORS["neutral_border"]), 1))
        painter.setBrush(QColor(tokens.COLORS["card_bg"]))
        painter.drawRoundedRect(rect, tokens.RADII["card"], tokens.RADII["card"])

        # 左侧 6px 状态灯
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(8, rect.height() / 2 - 3, 6, 6))

        font = painter.font()
        font.setPointSize(9)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(tokens.COLORS["neutral_helper"]))
        painter.drawText(QRectF(22, 7, rect.width() - 30, 16),
                         Qt.AlignLeft | Qt.AlignVCenter, self.class_type)

        font.setPointSize(12)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(tokens.COLORS["neutral_title"]))
        painter.drawText(QRectF(22, 25, rect.width() - 30, 24),
                         Qt.AlignLeft | Qt.AlignVCenter, self.title)

        font.setPointSize(9)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(QRectF(22, 49, rect.width() - 30, 14),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         tokens.NODE_STATUS_LABELS.get(self.status, "待执行"))

        # 阻断标记：两道竖线屏障；失败标记：叉号。二者与状态色一起构成视觉区分。
        painter.setPen(QPen(color, 2))
        if self.status == "blocked":
            x = rect.width() - 18
            painter.drawLine(x, 8, x, rect.height() - 8)
            painter.drawLine(x + 7, 8, x + 7, rect.height() - 8)
        elif self.status in ("failed", "rejected"):
            x = rect.width() - 20
            painter.drawLine(x, 10, x + 12, 22)
            painter.drawLine(x + 12, 10, x, 22)

        if self.isSelected():
            painter.setPen(QPen(QColor(tokens.PRIMARY), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1),
                                    tokens.RADII["card"], tokens.RADII["card"])

    def mousePressEvent(self, event) -> None:
        self._view.activate_node(self.node_id)
        super().mousePressEvent(event)


class LinkItem(QGraphicsItem):
    """带箭头的细连线：细线 + 末端三角箭头，均由 QPainter 绘制。"""

    def __init__(self, start: QPointF, end: QPointF) -> None:
        super().__init__()
        self._start = start
        self._end = end
        self.setZValue(-1)

    def boundingRect(self) -> QRectF:
        left = min(self._start.x(), self._end.x()) - 8
        top = min(self._start.y(), self._end.y()) - 8
        return QRectF(left, top,
                      abs(self._end.x() - self._start.x()) + 16,
                      abs(self._end.y() - self._start.y()) + 16)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(tokens.COLORS["neutral_disabled"]), 1.5)
        painter.setPen(pen)
        painter.drawLine(self._start, self._end)
        angle = math.atan2(self._end.y() - self._start.y(),
                           self._end.x() - self._start.x())
        size = 7
        for delta in (2.6, -2.6):
            a = angle + delta
            painter.drawLine(
                self._end,
                QPointF(self._end.x() - size * math.cos(a),
                        self._end.y() - size * math.sin(a)),
            )


class GraphView(QGraphicsView):
    """联动图：设置图、渲染节点卡与连线、消费进度事件、点击回调。"""

    nodeClicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("graphView")
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setMinimumHeight(180)
        self.setStyleSheet(tokens.GRAPH_VIEW_QSS)
        self._cards: list[NodeCardItem] = []
        self._link_items: list[LinkItem] = []
        self._statuses: dict[str, str] = {}
        self.on_node_clicked: Callable[[str], None] | None = None
        self.nodeClicked.connect(self._dispatch_click)

    # -- 点击联动 -----------------------------------------------------------
    def set_node_clicked_callback(self, cb: Callable[[str], None] | None) -> None:
        self.on_node_clicked = cb

    def _dispatch_click(self, node_id: str) -> None:
        cb = self.on_node_clicked
        if callable(cb):
            cb(node_id)

    def activate_node(self, node_id: str) -> None:
        """点击节点卡（或程序化选中）时触发联动回调。"""
        for card in self._cards:
            card.setSelected(card.node_id == node_id)
        self.nodeClicked.emit(str(node_id))

    def select_node(self, node_id: str) -> None:
        self.activate_node(node_id)

    # -- 图渲染 -------------------------------------------------------------
    def clear(self) -> None:
        scene = self.scene()
        if scene is not None:
            scene.clear()
        self._cards = []
        self._link_items = []
        self._statuses = {}

    def set_graph(self, graph, statuses: dict | None = None) -> list[str]:
        """按图渲染节点卡与连线；返回渲染出来的节点 id 列表。"""
        nodes, links = normalize_graph(graph)
        self.clear()
        scene = self.scene()

        positions: dict[str, QPointF] = {}
        for index, spec in enumerate(nodes):
            status = _norm_status(spec["status"])
            if statuses and spec["id"] in statuses:
                status = _norm_status(statuses[spec["id"]])
            spec["status"] = status
            card = NodeCardItem(spec, self)
            card.setPos(MARGIN + index * STEP_X, MARGIN)
            scene.addItem(card)
            self._cards.append(card)
            self._statuses[spec["id"]] = status
            positions[spec["id"]] = QPointF(MARGIN + index * STEP_X, MARGIN)

        for link in links:
            src = positions.get(link["from"])
            dst = positions.get(link["to"])
            if src is None or dst is None:
                continue
            start = QPointF(src.x() + CARD_W, src.y() + CARD_H / 2)
            end = QPointF(dst.x() - 2, dst.y() + CARD_H / 2)
            item = LinkItem(start, end)
            scene.addItem(item)
            self._link_items.append(item)

        # 未登记连线的图按节点顺序补一条链式连线，保证进度可视化始终可读
        if not self._link_items and len(self._cards) > 1:
            for i in range(len(self._cards) - 1):
                a = self._cards[i].pos()
                b = self._cards[i + 1].pos()
                item = LinkItem(
                    QPointF(a.x() + CARD_W, a.y() + CARD_H / 2),
                    QPointF(b.x() - 2, b.y() + CARD_H / 2),
                )
                scene.addItem(item)
                self._link_items.append(item)

        width = MARGIN * 2 + max(0, len(nodes) - 1) * STEP_X + CARD_W
        height = MARGIN * 2 + CARD_H
        scene.setSceneRect(0, 0, width, height)
        return self.node_ids()

    def node_items(self) -> list[NodeCardItem]:
        return list(self._cards)

    def link_items(self) -> list[LinkItem]:
        return list(self._link_items)

    def node_ids(self) -> list[str]:
        return [c.node_id for c in self._cards]

    def node_count(self) -> int:
        return len(self._cards)

    def statuses(self) -> dict[str, str]:
        return dict(self._statuses)

    def blocked_markers(self) -> list[str]:
        return [c.node_id for c in self._cards if c.status in ("blocked", "failed", "rejected")]

    def card_for(self, node_id: str) -> NodeCardItem | None:
        for card in self._cards:
            if card.node_id == str(node_id):
                return card
        return None

    def set_statuses(self, statuses: dict) -> None:
        for node_id, status in (statuses or {}).items():
            self._set_one_status(str(node_id), _norm_status(status))
        self.scene().update()

    # -- 进度事件 -----------------------------------------------------------
    def _set_one_status(self, node_id: str, status: str) -> None:
        card = self.card_for(node_id)
        if card is not None:
            card.set_status(status)
        self._statuses[node_id] = status

    def update_progress(self, event) -> None:
        """消费 itc.graph.events.ProgressEvent（也兼容 dict / 鸭子类型）。"""
        if isinstance(event, ProgressEvent):
            node_id = event.node_id
            status = event.status
            message = event.message
        elif isinstance(event, dict):
            node_id = event.get("node_id")
            status = event.get("status")
            message = event.get("message", "")
        else:
            node_id = getattr(event, "node_id", None)
            status = getattr(event, "status", None)
            message = getattr(event, "message", "")
        if node_id is None:
            return
        self._set_one_status(str(node_id), _norm_status(status))
        card = self.card_for(str(node_id))
        if card is not None and message:
            card.setToolTip(str(message))
        self.scene().update()
