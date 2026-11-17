"""联动图数据模型：节点 / 连线 / 图，以及连线输入与常量输入的精确区分。

对齐源机制：ComfyUI comfy_execution/graph_utils.py:1 is_link()、:83 Node、:106 serialize()。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def is_link(obj: Any) -> bool:
    """连线输入形如 [节点 id: str, 输出 socket: int/float]；其余一律是常量输入。

    精确复刻源机制：必须是长度 2 的 list，首元素为 str，次元素为 int 或 float。
    """
    if not isinstance(obj, list):
        return False
    if len(obj) != 2:
        return False
    if not isinstance(obj[0], str):
        return False
    if not isinstance(obj[1], int) and not isinstance(obj[1], float):
        return False
    return True


class GraphValidationError(ValueError):
    """图结构非法：连线指向不存在的节点、连线与端口登记不一致等。"""


class Node:
    def __init__(self, id, class_type, inputs=None, override_display_id=None):
        self.id = id
        self.class_type = class_type
        self.inputs = dict(inputs) if inputs else {}
        self.override_display_id = override_display_id

    def out(self, index):
        """返回一个可被 is_link 识别的连线输入值 [节点 id, socket]。"""
        return [self.id, index]

    def set_input(self, key, value):
        if value is None:
            self.inputs.pop(key, None)
        else:
            self.inputs[key] = value

    def get_input(self, key):
        return self.inputs.get(key)

    def set_override_display_id(self, override_display_id):
        self.override_display_id = override_display_id

    def serialize(self):
        """输出 {"class_type", "inputs"} 形态，与后端 prompt 结构一致。"""
        serialized = {
            "class_type": self.class_type,
            "inputs": dict(self.inputs),
        }
        if self.override_display_id is not None:
            serialized["override_display_id"] = self.override_display_id
        return serialized

    @classmethod
    def deserialize(cls, id, data):
        return cls(
            id,
            data["class_type"],
            dict(data.get("inputs", {})),
            data.get("override_display_id"),
        )

    def __eq__(self, other):
        if not isinstance(other, Node):
            return NotImplemented
        return self.id == other.id and self.serialize() == other.serialize()

    def __repr__(self):
        return f"Node(id={self.id!r}, class_type={self.class_type!r})"


@dataclass(frozen=True)
class Link:
    from_node_id: str
    from_socket: int | float
    to_node_id: str
    to_input: str

    def key(self):
        return (self.from_node_id, self.from_socket, self.to_node_id, self.to_input)

    def as_dict(self):
        return {
            "from_node_id": self.from_node_id,
            "from_socket": self.from_socket,
            "to_node_id": self.to_node_id,
            "to_input": self.to_input,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(d["from_node_id"], d["from_socket"], d["to_node_id"], d["to_input"])


class Graph:
    def __init__(self, nodes=None, links=None):
        self.nodes = {}
        self.links = []
        if nodes:
            items = nodes.items() if isinstance(nodes, dict) else ((n.id, n) for n in nodes)
            for nid, node in items:
                self.add_node(node if isinstance(node, Node) else Node.deserialize(nid, node))
        for link in links or []:
            self.add_link(link)

    def add_node(self, node):
        self.nodes[node.id] = node
        # 连线可能先于目标节点登记，补写端口，保证序列化往返与登记顺序无关
        for link in self.links:
            if link.to_node_id == node.id:
                node.set_input(link.to_input, [link.from_node_id, link.from_socket])
        return node

    def add_link(self, link):
        if isinstance(link, dict):
            link = Link.from_dict(link)
        # 同一对端口的重复连线只登记一次，避免重复传播
        if link.key() not in {existing.key() for existing in self.links}:
            self.links.append(link)
        target = self.nodes.get(link.to_node_id)
        if target is not None:
            target.set_input(link.to_input, [link.from_node_id, link.from_socket])
        return link

    def to_dict(self):
        return {
            "nodes": {nid: node.serialize() for nid, node in self.nodes.items()},
            "links": [link.as_dict() for link in self.links],
        }

    @classmethod
    def from_dict(cls, d):
        return cls(nodes=d.get("nodes", {}), links=d.get("links", []))

    def validate(self):
        for link in self.links:
            if link.from_node_id not in self.nodes:
                raise GraphValidationError(f"连线起点节点不存在：{link.from_node_id}")
            if link.to_node_id not in self.nodes:
                raise GraphValidationError(f"连线终点节点不存在：{link.to_node_id}")
            value = self.nodes[link.to_node_id].get_input(link.to_input)
            if not is_link(value) or value[0] != link.from_node_id or value[1] != link.from_socket:
                raise GraphValidationError(
                    f"节点 {link.to_node_id} 端口 {link.to_input} 的连线登记不一致"
                )
        for nid, node in self.nodes.items():
            for key, value in node.inputs.items():
                if is_link(value) and value[0] not in self.nodes:
                    raise GraphValidationError(
                        f"节点 {nid} 端口 {key} 连线指向不存在的节点 {value[0]}"
                    )
        return True

    def downstream(self, node_id):
        result = []
        for link in self.links:
            if link.from_node_id == node_id and link.to_node_id not in result:
                result.append(link.to_node_id)
        # 兼容只登记了 inputs 连线、未显式 add_link 的图
        for nid, node in self.nodes.items():
            for value in node.inputs.values():
                if is_link(value) and value[0] == node_id and nid not in result:
                    result.append(nid)
        return result

    def node_ids(self):
        return list(self.nodes.keys())
