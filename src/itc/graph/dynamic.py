"""运行时动态插节点：父链回溯、按患者数据决定节点集合、插入后依赖重排。

对齐源机制：ComfyUI comfy_execution/graph.py:21 DynamicPrompt、
:40 add_ephemeral_node()、:45 get_real_node_id()、:53 get_display_node_id()。
"""
from __future__ import annotations

from itc.graph.model import is_link
from itc.graph.registry import NODE_TYPES
from itc.graph.scheduler import (
    DependencyCycleError,
    NodeNotFoundError,
    _coerce_prompt,
    ready_order,
)


class OutOfOrderInsertionError(Exception):
    """父节点尚未完成就尝试在其下插入节点。"""


class DynamicPrompt:
    def __init__(self, original_prompt: dict):
        self.original_prompt = dict(original_prompt or {})
        self.ephemeral_prompt = {}
        self.ephemeral_parents = {}
        self.ephemeral_display = {}
        self.ephemeral_reasons = {}
        self._triggers = set()

    def get_node(self, node_id):
        if node_id in self.ephemeral_prompt:
            return self.ephemeral_prompt[node_id]
        if node_id in self.original_prompt:
            return self.original_prompt[node_id]
        raise NodeNotFoundError(f"节点 {node_id} 不存在")

    def has_node(self, node_id):
        return node_id in self.original_prompt or node_id in self.ephemeral_prompt

    def add_ephemeral_node(self, node_id, node_info, parent_id, display_id=None,
                           reason="", completed_parents=None):
        """运行时挂入节点；completed_parents 不含 parent_id 时抛乱序插入错误。"""
        if completed_parents is not None and parent_id not in set(completed_parents):
            raise OutOfOrderInsertionError(
                f"父节点 {parent_id} 尚未完成，不能在其下插入动态节点 {node_id}"
            )
        self.ephemeral_prompt[node_id] = dict(node_info)
        self.ephemeral_parents[node_id] = parent_id
        self.ephemeral_reasons[node_id] = reason
        if display_id is not None:
            self.ephemeral_display[node_id] = display_id
        return node_id

    def get_real_node_id(self, node_id):
        """沿父链回溯到真实（原始）节点 id；seen 防止异常父链导致死循环。"""
        seen = set()
        while node_id in self.ephemeral_parents and node_id not in seen:
            seen.add(node_id)
            node_id = self.ephemeral_parents[node_id]
        return node_id

    def get_parent_node_id(self, node_id):
        return self.ephemeral_parents.get(node_id)

    def get_display_node_id(self, node_id):
        seen = set()
        while node_id in self.ephemeral_display and node_id not in seen:
            seen.add(node_id)
            node_id = self.ephemeral_display[node_id]
        return node_id

    def all_node_ids(self):
        return set(self.original_prompt) | set(self.ephemeral_prompt)

    def get_original_prompt(self):
        return self.original_prompt

    def has_trigger(self, key):
        return key in self._triggers

    def mark_trigger(self, key):
        self._triggers.add(key)


def decide_nodes(patient_data: dict) -> list[str]:
    """按患者当下数据决定处置节点集合。

    - 无 executing_orders -> 不生成 change_site
    - 有 pending_specimens -> 生成 specimen_route
    - 有床位费明细 -> 生成 bed_fee_hint
    """
    data = patient_data or {}
    orders = data.get("executing_orders") or []
    specimens = data.get("pending_specimens") or []
    charges = data.get("bed_charges") or data.get("charges") or []
    nodes = []
    if orders:
        nodes.append("change_site")
    if specimens:
        nodes.append("specimen_route")
    if charges:
        nodes.append("bed_fee_hint")
    return nodes


def _pick_parent(dynprompt, completed):
    """选一个已完成的原始节点作父；没有则仍返回原始节点，交给乱序检查报错。"""
    original = dynprompt.get_original_prompt()
    for node_id in original:
        if node_id in completed:
            return node_id
    for node_id in original:
        return node_id
    return "root"


def insert_disposition_nodes(dynprompt, patient_data, completed_parents, registry=None):
    """按患者数据插入处置节点；以触发条件键去重，重复触发不插重复节点。"""
    completed = set(completed_parents or [])
    parent_id = _pick_parent(dynprompt, completed)
    types = registry if registry is not None else NODE_TYPES
    inserted = []
    for type_id in decide_nodes(patient_data):
        trigger = (parent_id, type_id)
        node_id = f"dyn:{parent_id}:{type_id}"
        if dynprompt.has_trigger(trigger) or dynprompt.has_node(node_id):
            continue
        if type_id not in types:
            continue
        info = {
            "class_type": type_id,
            "inputs": {"source": [parent_id, 0]},
        }
        dynprompt.add_ephemeral_node(
            node_id,
            info,
            parent_id,
            display_id=node_id,
            reason=f"患者当下数据触发：{type_id}",
            completed_parents=completed,
        )
        dynprompt.mark_trigger(trigger)
        inserted.append(node_id)
    return inserted


def reorder_after_insert(graph, new_node_ids) -> list[str]:
    """插入后依赖重排：以既有节点稳定拓扑序为骨架，把新节点按依赖插入。

    既有节点相对顺序保持不变；新节点去重后放到其所有上游之后。
    """
    view = _coerce_prompt(graph)
    all_ids = view.all_node_ids()
    new_list, seen = [], set()
    for node_id in new_node_ids:
        if node_id in all_ids and node_id not in seen:
            seen.add(node_id)
            new_list.append(node_id)

    # 既有骨架：剔除新节点，并丢弃指向新节点的连线
    skeleton_prompt = {}
    for node_id in all_ids:
        if node_id in seen:
            continue
        info = view.get_node(node_id)
        inputs = {
            key: value
            for key, value in info["inputs"].items()
            if not (is_link(value) and value[0] in seen)
        }
        skeleton_prompt[node_id] = {
            "class_type": info["class_type"],
            "inputs": inputs,
        }
    skeleton = ready_order(skeleton_prompt) if skeleton_prompt else []

    result = list(skeleton)
    remaining = list(new_list)
    while remaining:
        progressed = False
        for node_id in list(remaining):
            info = view.get_node(node_id)
            deps = [
                value[0]
                for value in info["inputs"].values()
                if is_link(value) and value[0] in all_ids
            ]
            if all(dep in result for dep in deps):
                positions = [result.index(dep) for dep in deps if dep in result]
                insert_at = max(positions, default=-1) + 1
                result.insert(insert_at, node_id)
                remaining.remove(node_id)
                progressed = True
        if not progressed:
            raise DependencyCycleError(
                "动态节点之间存在无法满足的依赖（疑似环）：" + ", ".join(remaining)
            )
    return result
