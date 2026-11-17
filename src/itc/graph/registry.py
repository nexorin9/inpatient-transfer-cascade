"""节点类型注册表：类型 id / 必填输入端口 / 输出端口 / 中文显示名 / 是否可写回。

G1 与 M7 处置类型目录共用同一份定义（M7 只读 NODE_TYPES，不修改本文件）。
必填端口值含义：link 表示必须连线；str/int/... 表示必须常量且类型匹配；any 表示不限。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NodeType:
    type_id: str
    display_name: str
    required_inputs: dict = field(default_factory=dict)
    output_ports: tuple = ()
    writable: bool = False

    def as_dict(self):
        return {
            "type_id": self.type_id,
            "display_name": self.display_name,
            "required_inputs": dict(self.required_inputs),
            "output_ports": list(self.output_ports),
            "writable": self.writable,
        }


def _t(type_id, display_name, required_inputs=None, output_ports=("out",), writable=False):
    return NodeType(
        type_id,
        display_name,
        dict(required_inputs or {}),
        tuple(output_ports),
        writable,
    )


NODE_TYPES = {
    "fetch_orders": _t("fetch_orders", "取在执行长嘱", {"inpatient_no": "str"}),
    "fetch_specimens": _t("fetch_specimens", "取待送标本", {"inpatient_no": "str"}),
    "fetch_charges": _t("fetch_charges", "取床位费明细", {"inpatient_no": "str"}),
    "rule_judge": _t("rule_judge", "规则判定", {"orders": "link"}),
    "model_judge": _t("model_judge", "模型判定", {"orders": "link"}),
    "change_site": _t("change_site", "改执行地点", {"source": "link"}, writable=True),
    "specimen_route": _t("specimen_route", "送标本去向", {"source": "link"}),
    "bed_fee_hint": _t("bed_fee_hint", "床位费归属提示", {"source": "link"}),
    "stop_draft": _t("stop_draft", "停嘱草稿", {"source": "link"}, writable=True),
    "external_block": _t("external_block", "外部人工阻塞", {}),
}


def register_node_type(node_type):
    """登记（或覆盖）一个节点类型，返回登记后的对象。"""
    if not isinstance(node_type, NodeType):
        raise TypeError("register_node_type 只接受 NodeType 实例")
    NODE_TYPES[node_type.type_id] = node_type
    return node_type


def get_node_type(type_id):
    """按类型 id 取节点类型；未知返回 None（由 unknown_type_fallback 兜底）。"""
    return NODE_TYPES.get(type_id)


def unknown_type_fallback(type_id):
    """未知类型兜底：不猜必填端口、不可写回，供兼容旧图或扩展类型使用。"""
    return NodeType(
        type_id=type_id or "unknown",
        display_name=f"未知节点类型（{type_id}）",
        required_inputs={},
        output_ports=("out",),
        writable=False,
    )
