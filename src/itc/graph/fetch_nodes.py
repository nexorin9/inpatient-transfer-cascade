"""取数节点工厂与 reader 接线（图节点 ↔ his/reader 的接缝）。

只依赖底座 domain.models / store.db 与 G1 的 idempotency.business_hash；
不 import H/M/U 组模块。取数节点只做「按参数取回 + 按字段规范解析」：
解析失败抛 FetchNodeError 并阻断下游；空结果为成功且下游不长节点。
"""
from __future__ import annotations

import json
from typing import Any

from itc.domain.models import BedCharge, ExecutingOrder, Specimen
from itc.graph.idempotency import business_hash

# 资源类型 → reader 方法名 / 领域模型 / 节点 class_type（与 G1 NODE_TYPES 对齐）
RESOURCE_METHOD = {
    "orders": "read_executing_orders",
    "specimens": "read_pending_specimens",
    "charges": "read_bed_charges",
}
RESOURCE_MODEL = {
    "orders": ExecutingOrder,
    "specimens": Specimen,
    "charges": BedCharge,
}
RESOURCE_CLASS_TYPE = {
    "orders": "fetch_orders",
    "specimens": "fetch_specimens",
    "charges": "fetch_charges",
}


class FetchNodeError(RuntimeError):
    """取数节点解析失败：携带节点 id、可读原因与下游阻断清单。"""

    def __init__(
        self,
        message: str,
        *,
        node_id: str = "",
        downstream: list[str] | None = None,
        reason: str = "",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.node_id = node_id
        self.downstream = list(downstream or [])
        self.reason = reason or message
        self.retryable = retryable

    def blocks(self, node_id: str) -> bool:
        return node_id in self.downstream

    def as_dict(self) -> dict:
        return {
            "error": "FetchNodeError",
            "message": self.message,
            "node_id": self.node_id,
            "reason": self.reason,
            "downstream": list(self.downstream),
            "retryable": self.retryable,
        }


def fetch_cache_key(resource_type: str, inpatient_no: str, filters: dict | None = None) -> str:
    """取数节点缓存键：节点签名 + G1 业务哈希，不含时间戳与重试次数。"""
    stable_filters = json.dumps(dict(sorted((filters or {}).items())), ensure_ascii=False, sort_keys=True)
    digest = business_hash(
        resource_type=str(resource_type),
        inpatient_no=str(inpatient_no),
        filters=stable_filters,
    )
    return f"fetch:{digest}"


def make_fetch_node(resource_type: str, inpatient_no: str, filters: dict | None = None) -> dict:
    """生成取数节点定义：含 inpatient_no、过滤条件、节点签名与业务哈希缓存键。"""
    if resource_type not in RESOURCE_METHOD:
        raise ValueError(f"未知取数资源类型：{resource_type}")
    normalized_filters = dict(filters or {})
    node = {
        "id": f"{RESOURCE_CLASS_TYPE[resource_type]}:{inpatient_no}",
        "class_type": RESOURCE_CLASS_TYPE[resource_type],
        "resource_type": resource_type,
        "inputs": {
            "inpatient_no": str(inpatient_no),
            "filters": normalized_filters,
        },
        "downstream": [f"judge_{resource_type}"],
        "cache_key": fetch_cache_key(resource_type, inpatient_no, normalized_filters),
    }
    return node


def _parse_row(row: Any, model) -> Any:
    """把 reader 返回的一行解析为领域对象；字段缺失/类型错误由调用方包成 FetchNodeError。"""
    if isinstance(row, model):
        return row
    if isinstance(row, dict):
        return model.from_dict(row)
    if hasattr(row, "to_dict"):
        return model.from_dict(row.to_dict())
    raise ValueError(f"无法解析的行类型：{type(row).__name__}")


def _matches(obj: Any, filters: dict) -> bool:
    data = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
    for key, expected in filters.items():
        if data.get(key) != expected:
            return False
    return True


def execute_fetch_node(node: dict, reader) -> list:
    """执行取数节点：调用注入 reader 的对应方法，按字段规范解析，返回领域对象列表。

    reader 方法异常原样抛出（交给节点级重试/熔断层分类）；行解析失败抛
    FetchNodeError 并附下游阻断；空结果返回 [] 表示节点成功。
    """
    resource_type = node.get("resource_type")
    if resource_type not in RESOURCE_METHOD:
        raise FetchNodeError(
            f"节点缺少合法的 resource_type：{resource_type}",
            node_id=node.get("id", ""),
            downstream=node.get("downstream"),
        )
    method_name = RESOURCE_METHOD[resource_type]
    model = RESOURCE_MODEL[resource_type]
    inputs = node.get("inputs", {})
    inpatient_no = inputs.get("inpatient_no")
    downstream = list(node.get("downstream") or [])
    node_id = node.get("id", "")

    if not inpatient_no:
        raise FetchNodeError(
            "取数节点缺少 inpatient_no",
            node_id=node_id,
            downstream=downstream,
        )

    try:
        method = getattr(reader, method_name)
    except AttributeError as exc:
        raise FetchNodeError(
            f"reader 缺少方法 {method_name}",
            node_id=node_id,
            downstream=downstream,
        ) from exc

    # reader 方法异常（网络 / 5xx / 超时等）原样抛给重试层，不在这里吞掉
    rows = method(inpatient_no)

    if rows is None or not isinstance(rows, (list, tuple)):
        raise FetchNodeError(
            f"{method_name} 返回非列表，无法按字段规范解析",
            node_id=node_id,
            downstream=downstream,
        )

    parsed: list = []
    for index, row in enumerate(rows):
        try:
            parsed.append(_parse_row(row, model))
        except Exception as exc:
            raise FetchNodeError(
                f"第 {index} 行解析失败：{exc}",
                node_id=node_id,
                downstream=downstream,
                reason=f"取数结果解析失败（{method_name} 第 {index} 行）：{exc}",
            ) from exc

    filters = inputs.get("filters") or {}
    if filters:
        parsed = [obj for obj in parsed if _matches(obj, filters)]
    return parsed


def downstream_nodes(result: list, node: dict | None = None) -> list[str]:
    """按取数结果决定下游节点：空结果不长节点；非空返回待生成的判定节点。"""
    if not result:
        return []
    if node is not None and node.get("downstream"):
        return list(node["downstream"])
    return ["judge"]


class FetchCache:
    """取数节点产出缓存：键含 G1 业务哈希，供局部重跑复用（命中不再调用 reader）。"""

    def __init__(self):
        self._store: dict[str, list] = {}
        self.hits = 0
        self.misses = 0

    def get_or_execute(self, node: dict, reader) -> list:
        key = node.get("cache_key") or fetch_cache_key(
            node.get("resource_type", ""),
            node.get("inputs", {}).get("inpatient_no", ""),
            node.get("inputs", {}).get("filters"),
        )
        if key in self._store:
            self.hits += 1
            return list(self._store[key])
        self.misses += 1
        output = execute_fetch_node(node, reader)
        self._store[key] = list(output)
        return output

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0

    def summary(self) -> dict:
        return {"entries": len(self._store), "hits": self.hits, "misses": self.misses}


class _CountingReader:
    """reader 代理：逐方法计数，供局部重跑 / 重复入图不取数的断言。"""

    def __init__(self, reader):
        self._reader = reader
        self.counts: dict[str, int] = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def _invoke(self, name: str, *args, **kwargs):
        self.counts[name] = self.counts.get(name, 0) + 1
        self.calls.append((name, args, kwargs))
        return getattr(self._reader, name)(*args, **kwargs)

    def read_executing_orders(self, *args, **kwargs):
        return self._invoke("read_executing_orders", *args, **kwargs)

    def read_pending_specimens(self, *args, **kwargs):
        return self._invoke("read_pending_specimens", *args, **kwargs)

    def read_bed_charges(self, *args, **kwargs):
        return self._invoke("read_bed_charges", *args, **kwargs)

    def call_count(self, method: str | None = None) -> int:
        if method is not None:
            return self.counts.get(method, 0)
        return sum(self.counts.values())

    def __getattr__(self, name):
        # 其它属性/方法透传给底层 reader（不计数）
        return getattr(self._reader, name)


def counting_reader(reader):
    """包装 reader，返回带 counts / call_count() 的计数代理。"""
    return _CountingReader(reader)
