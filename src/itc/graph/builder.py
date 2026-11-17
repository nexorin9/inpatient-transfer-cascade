"""图构建器：把业务模板装配成可执行图，构建期完成必填与端口类型校验。

对齐源机制：ComfyUI comfy_execution/graph_utils.py:13 GraphBuilder、:60 finalize()。
"""
from __future__ import annotations

from itc.graph.model import Graph, Link, Node, is_link
from itc.graph.ports import PortValidationError, validate_ports


class GraphBuildError(Exception):
    """构建期校验失败：缺必填输入、端口类型不匹配、连线指向不存在节点。"""


class GraphBuilder:
    def __init__(self, prefix=""):
        self.prefix = prefix
        self.nodes = {}
        self.id_gen = 1

    def node(self, class_type, id=None, **kwargs):
        if id is None:
            id = str(self.id_gen)
            self.id_gen += 1
        id = self.prefix + str(id)
        if id in self.nodes:
            return self.nodes[id]
        node = Node(id, class_type, kwargs)
        self.nodes[id] = node
        return node

    def lookup_node(self, id):
        return self.nodes.get(self.prefix + str(id))

    def finalize(self):
        """构建完成校验：缺必填端口 / 类型不匹配 / 悬空连线都在入队前报出。

        返回 {节点 id: node.serialize()} 形态，与源机制 finalize() 一致。
        """
        problems = []
        output = {}
        for node_id, node in self.nodes.items():
            try:
                validate_ports(node.class_type, node.inputs)
            except PortValidationError as exc:
                problems.append(f"节点 {node_id}({node.class_type})：{exc}")
            for key, value in node.inputs.items():
                if is_link(value) and value[0] not in self.nodes:
                    problems.append(
                        f"节点 {node_id} 端口 {key} 连线指向不存在的节点 {value[0]}"
                    )
            output[node_id] = node.serialize()
        if problems:
            raise GraphBuildError("构建期校验失败：" + "；".join(problems))
        return output

    def replace_node_output(self, node_id, index, new_value):
        """替换某节点某输出的所有引用；new_value 为 None 时删除该输入。"""
        node_id = self.prefix + str(node_id)
        to_remove = []
        for node in self.nodes.values():
            for key, value in list(node.inputs.items()):
                if is_link(value) and value[0] == node_id and value[1] == index:
                    if new_value is None:
                        to_remove.append((node, key))
                    else:
                        node.inputs[key] = new_value
        for node, key in to_remove:
            del node.inputs[key]

    def remove_node(self, id):
        del self.nodes[self.prefix + str(id)]

    def build(self):
        """校验通过后产出 Graph 对象。"""
        self.finalize()
        graph = Graph()
        for node in self.nodes.values():
            graph.add_node(
                Node(node.id, node.class_type, dict(node.inputs), node.override_display_id)
            )
        for node_id, node in self.nodes.items():
            for key, value in node.inputs.items():
                if is_link(value):
                    graph.add_link(Link(value[0], value[1], node_id, key))
        graph.validate()
        return graph
