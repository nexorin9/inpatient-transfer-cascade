"""处置动作类型目录（信息科可维护，对应源产品 object_info 的目录语义）。

内置 4 类：改执行地点 / 送标本去向 / 床位费归属提示 / 停嘱草稿，与 G1 的
itc.graph.registry.NODE_TYPES 共用类型定义（只读 import，不修改 G1 文件）。
信息科用 JSON 追加自定义类型，refresh 后目录可见；图中出现未注册类型时只把
该节点标为待人工处置，其余节点照常执行，不整图失败。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from itc.graph.registry import NODE_TYPES


class ActionCatalogError(RuntimeError):
    """自定义处置动作类型目录缺失 / 损坏 / 结构非法。"""


@dataclass(frozen=True)
class ActionType:
    """一条处置动作类型定义。

    applies_to 为适用处置对象（order / specimen / charge）；writable 表示是否
    可写回；default_params 为信息科可配置的默认参数。
    """

    type_id: str
    display_name: str
    applies_to: str = ""
    writable: bool = False
    default_params: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "type_id": self.type_id,
            "display_name": self.display_name,
            "applies_to": self.applies_to,
            "writable": self.writable,
            "default_params": dict(self.default_params),
        }


# 内置 4 类：type_id 对齐 G1 NODE_TYPES，显示名与 writable 一律从 NODE_TYPES 读取。
_BUILTIN_SPECS = (
    ("change_site", "order", {"reason": "TRANSFER_CASCADE"}),
    ("specimen_route", "specimen", {}),
    ("bed_fee_hint", "charge", {
        "hint_only": True,
        "disclaimer": "资产层未给换算规则，此处为一致性提示",
    }),
    ("stop_draft", "order", {"draft_type": "STOP", "status": "DRAFT"}),
)


class ActionTypeRegistry:
    """处置动作类型目录：注册、自定义加载、刷新、查询与未知类型兜底。"""

    def __init__(self, custom_path=None):
        self._types = {}
        self._persistent = {}
        self._custom_path = None
        self._load_builtin()
        if custom_path is not None:
            self.load_custom(custom_path)

    def _load_builtin(self):
        """从 G1 NODE_TYPES 只读构建内置 4 类，绝不写回 G1 的字典。"""
        for type_id, applies_to, params in _BUILTIN_SPECS:
            node = NODE_TYPES.get(type_id)
            self._types[type_id] = ActionType(
                type_id=type_id,
                display_name=node.display_name if node else type_id,
                applies_to=applies_to,
                writable=bool(node.writable) if node else False,
                default_params=dict(params),
            )

    def register(self, action_type):
        """登记或覆盖一条类型；显式登记的类型在 refresh 后仍会保留。"""
        at = action_type if isinstance(action_type, ActionType) else ActionType(**dict(action_type))
        self._types[at.type_id] = at
        self._persistent[at.type_id] = at
        return at

    def load_custom(self, path):
        """从信息科 JSON 追加类型，返回本次加载的 ActionType 列表。

        文件不存在时视为尚无自定义类型，返回空列表；存在但损坏 / 结构非法则
        抛 ActionCatalogError，由信息科修正目录，不静默伪造类型。
        """
        self._custom_path = Path(path)
        if not self._custom_path.is_file():
            return []
        try:
            data = json.loads(self._custom_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise ActionCatalogError(f"自定义处置动作类型目录读取失败：{exc}") from exc
        if isinstance(data, dict):
            raw = data.get("action_types", data.get("types"))
            if raw is None and "type_id" in data:
                raw = [data]
        elif isinstance(data, list):
            raw = data
        else:
            raw = None
        if not isinstance(raw, list):
            raise ActionCatalogError("自定义类型目录需为数组或含 action_types 数组的对象")
        loaded = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("type_id"):
                raise ActionCatalogError("自定义类型条目缺少 type_id")
            at = ActionType(
                type_id=str(item["type_id"]),
                display_name=str(item.get("display_name") or item["type_id"]),
                applies_to=str(item.get("applies_to") or ""),
                writable=bool(item.get("writable", False)),
                default_params=dict(item.get("default_params") or {}),
            )
            self._types[at.type_id] = at
            loaded.append(at)
        return loaded

    def refresh(self):
        """重建目录：内置 4 类 + 显式登记类型 + 从自定义 JSON 重载。

        信息科改完 JSON 后调用本方法即可见新增类型；返回当前全部类型。
        """
        self._types = {}
        self._load_builtin()
        for at in self._persistent.values():
            self._types[at.type_id] = at
        if self._custom_path is not None and self._custom_path.is_file():
            self.load_custom(self._custom_path)
        return self.all_types()

    def get(self, type_id):
        """按 type_id 取类型；未注册返回 None（不猜，由兜底处理）。"""
        return self._types.get(type_id)

    def all_types(self):
        """返回全部已注册类型（按 type_id 稳定排序）。"""
        return [self._types[k] for k in sorted(self._types)]

    def unknown_type_fallback(self, type_id):
        """未知类型兜底：标为待人工处置、不可写回，不注册进目录。"""
        return ActionType(
            type_id=type_id or "unknown",
            display_name=f"待人工处置（未注册类型：{type_id}）",
            applies_to="",
            writable=False,
            default_params={
                "manual": True,
                "reason": f"图中出现未注册处置动作类型 {type_id}，该节点标为待人工处置，不自动执行",
            },
        )

    def classify_types(self, type_ids):
        """把图中节点类型分成可执行与待人工处置两类，不整图失败。

        已在本目录或 G1 NODE_TYPES 注册的类型进 executable 照常执行；只有两处
        都查不到的未注册类型进 manual 并给出兜底定义，不因单个未知节点整图失败。
        """
        executable, manual = [], []
        for type_id in type_ids:
            if self.get(type_id) is not None or type_id in NODE_TYPES:
                executable.append(type_id)
            else:
                manual.append(type_id)
        return {
            "executable": executable,
            "manual": manual,
            "fallbacks": {tid: self.unknown_type_fallback(tid).as_dict() for tid in manual},
            "graph_ok": True,
        }
