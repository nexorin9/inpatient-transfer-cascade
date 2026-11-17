"""依赖就绪调度：挡数计数、稳定拓扑序、环检测、必填输入校验、外部阻塞。

对齐源机制：ComfyUI comfy_execution/graph.py:110-111 blockCount / blocking、
:168 add_external_block()、:181 get_ready_nodes()、:184 pop_node()。
"""
from __future__ import annotations

from itc.graph.model import is_link
from itc.graph.ports import PortValidationError, validate_ports


class DependencyCycleError(Exception):
    """图中存在依赖环（含自环），无法产出就绪顺序。"""


class NodeInputError(Exception):
    """节点必填输入缺失或输入非法。"""


class NodeNotFoundError(Exception):
    """引用的节点不存在。"""


class _PromptView:
    """把各种图来源统一成 {节点 id: {"class_type", "inputs"}} 视图。"""

    def __init__(self, nodes):
        self._nodes = nodes

    def get_node(self, node_id):
        if node_id not in self._nodes:
            raise NodeNotFoundError(f"节点 {node_id} 不存在")
        return self._nodes[node_id]

    def has_node(self, node_id):
        return node_id in self._nodes

    def all_node_ids(self):
        return set(self._nodes)


def _as_node_info(data):
    return {
        "class_type": data.get("class_type", ""),
        "inputs": dict(data.get("inputs", {})),
    }


def _coerce_prompt(source):
    """接受 Graph / DynamicPrompt / prompt dict / _PromptView，统一成可读视图。"""
    if isinstance(source, _PromptView):
        return source
    if isinstance(source, dict):
        return _PromptView({k: _as_node_info(v) for k, v in source.items()})
    if hasattr(source, "get_node") and hasattr(source, "all_node_ids"):
        return source  # DynamicPrompt 自带 get_node / has_node / all_node_ids
    if hasattr(source, "to_dict"):
        d = source.to_dict()
        return _PromptView({k: _as_node_info(v) for k, v in d.get("nodes", {}).items()})
    if hasattr(source, "nodes"):
        return _PromptView({k: _as_node_info(n.serialize()) for k, n in source.nodes.items()})
    raise TypeError(f"不支持的图来源类型：{type(source)!r}")


class TopologicalSort:
    def __init__(self, graph_or_dynprompt):
        self.view = _coerce_prompt(graph_or_dynprompt)
        self.pendingNodes = {}
        self.blockCount = {}
        self.blocking = {}
        self.externalBlocks = 0
        self._cached = set()

    def is_cached(self, id):
        return id in self._cached

    def get_input_info(self, unique_id, input_name):
        return None

    def _ensure_pending(self, node_id):
        """最小登记：显式 add_strong_link 时保证两端节点计数结构存在。"""
        if node_id not in self.pendingNodes:
            if not self.view.has_node(node_id):
                raise NodeNotFoundError(f"节点 {node_id} 不存在")
            self.pendingNodes[node_id] = True
            self.blockCount[node_id] = 0
            self.blocking[node_id] = {}

    def add_node(self, node_unique_id, include_lazy=False):
        node_ids = [node_unique_id]
        links = []
        while node_ids:
            unique_id = node_ids.pop()
            if unique_id in self.pendingNodes:
                continue
            info = self.view.get_node(unique_id)
            self.pendingNodes[unique_id] = True
            self.blockCount[unique_id] = 0
            self.blocking[unique_id] = {}
            for input_name, value in info["inputs"].items():
                if is_link(value):
                    from_node_id, from_socket = value
                    if not self.view.has_node(from_node_id):
                        raise NodeInputError(
                            f"节点 {unique_id} 端口 {input_name} 连线指向不存在的节点 {from_node_id}"
                        )
                    if not self.is_cached(from_node_id):
                        node_ids.append(from_node_id)
                    links.append((from_node_id, from_socket, unique_id))
        for link in links:
            self.add_strong_link(*link)

    def add_strong_link(self, from_id, socket, to_id):
        if not self.is_cached(from_id):
            self._ensure_pending(from_id)
            self._ensure_pending(to_id)
            if to_id not in self.blocking[from_id]:
                self.blocking[from_id][to_id] = {}
                # 同一对节点重复连线只记一次挡数，避免两次传播
                self.blockCount[to_id] += 1
            self.blocking[from_id][to_id][socket] = True

    def get_ready_nodes(self):
        return [node_id for node_id in self.pendingNodes if self.blockCount[node_id] == 0]

    def pop_node(self, unique_id):
        del self.pendingNodes[unique_id]
        for blocked_node_id in self.blocking[unique_id]:
            self.blockCount[blocked_node_id] -= 1
        del self.blocking[unique_id]

    def is_empty(self):
        return len(self.pendingNodes) == 0

    def add_external_block(self, node_id):
        if node_id not in self.blockCount:
            raise NodeNotFoundError(f"不能对未入队节点 {node_id} 添加外部阻塞")
        self.externalBlocks += 1
        self.blockCount[node_id] += 1

        def unblock():
            self.externalBlocks -= 1
            self.blockCount[node_id] -= 1
            return True

        return unblock


def _dependency_edges(view):
    """构造 from -> [to] 依赖边（to 的输入连线来自 from）。"""
    ids = sorted(view.all_node_ids())
    edges = {nid: set() for nid in ids}
    for nid in ids:
        for value in view.get_node(nid)["inputs"].values():
            if is_link(value) and value[0] in edges:
                edges[value[0]].add(nid)
    return {nid: sorted(targets) for nid, targets in edges.items()}


def detect_cycle(graph):
    """返回环上节点序列（如 [A, B, C] 表示 A->B->C->A）；无环返回 []。含自环。"""
    view = _coerce_prompt(graph)
    edges = _dependency_edges(view)
    visited = set()
    on_stack = set()
    stack = []
    found = []

    def dfs(node):
        visited.add(node)
        on_stack.add(node)
        stack.append(node)
        for nxt in edges[node]:
            if nxt not in visited:
                if dfs(nxt):
                    return True
            elif nxt in on_stack:
                found.extend(stack[stack.index(nxt):])
                return True
        stack.pop()
        on_stack.discard(node)
        return False

    for node_id in sorted(edges):
        if node_id not in visited and dfs(node_id):
            return found
    return []


def _cycle_message(cycle):
    if not cycle:
        return "检测到依赖环"
    path = " -> ".join(list(cycle) + [cycle[0]])
    return f"检测到依赖环：{path}（环上节点：{', '.join(cycle)}）"


def ready_order(graph):
    """按挡数计数产出稳定拓扑序；存在环时抛 DependencyCycleError 并给出环上节点序列。"""
    view = _coerce_prompt(graph)
    ts = TopologicalSort(view)
    for node_id in sorted(view.all_node_ids()):
        ts.add_node(node_id)
    order = []
    while not ts.is_empty():
        ready = ts.get_ready_nodes()
        if not ready:
            raise DependencyCycleError(_cycle_message(detect_cycle(graph)))
        # 每次取 id 最小的就绪节点，保证输出稳定且可复跑
        node_id = min(ready)
        order.append(node_id)
        ts.pop_node(node_id)
    return order


def validate_required_inputs(graph):
    """入队前检查每个节点必填输入端口是否齐备、类型是否匹配。"""
    view = _coerce_prompt(graph)
    problems = []
    for node_id in sorted(view.all_node_ids()):
        info = view.get_node(node_id)
        try:
            validate_ports(info["class_type"], info["inputs"])
        except PortValidationError as exc:
            problems.append(f"节点 {node_id}({info['class_type']})：{exc}")
    if problems:
        raise NodeInputError("必填输入校验失败：" + "；".join(problems))
    return True
