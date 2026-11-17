"""输入端口：区分连线输入与常量输入，并在序列化时保留两类标记。

对齐源机制：ComfyUI comfy_execution/graph_utils.py:1 is_link()。
"""
from __future__ import annotations

from dataclasses import dataclass

from itc.graph.model import is_link
from itc.graph.registry import get_node_type, unknown_type_fallback


class PortValidationError(ValueError):
    """必填端口缺失或端口类型不匹配。"""


@dataclass(frozen=True)
class Port:
    name: str
    type: str = "any"
    required: bool = True
    link: bool = True

    def as_dict(self):
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "link": self.link,
        }


def is_link_input(v):
    """连线输入：[节点 id, int/float socket]。"""
    return is_link(v)


def is_constant_input(v):
    """常量输入：一切非连线输入。"""
    return not is_link(v)


def serialize_inputs(inputs):
    """序列化输入并标注 link_ports / constant_ports 两类端口。"""
    inputs = dict(inputs or {})
    return {
        "inputs": inputs,
        "link_ports": sorted(k for k, v in inputs.items() if is_link_input(v)),
        "constant_ports": sorted(k for k, v in inputs.items() if is_constant_input(v)),
    }


def _ports_of(spec):
    ports = []
    for name, kind in (spec.required_inputs or {}).items():
        # link 类型端口期望连线输入；其余端口期望常量输入
        ports.append(Port(name=name, type=kind, required=True, link=(kind == "link")))
    return ports


def _class_type_of(node):
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return node.get("class_type", "")
    return getattr(node, "class_type", "")


def _type_matches(kind, value):
    if kind == "str":
        return isinstance(value, str)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return isinstance(value, float)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "dict":
        return isinstance(value, dict)
    if kind == "list":
        return isinstance(value, list)
    return True


def validate_ports(node, inputs):
    """校验必填输入端口是否齐备、类型是否匹配；失败时指出缺哪个端口。"""
    class_type = _class_type_of(node)
    spec = get_node_type(class_type) or unknown_type_fallback(class_type)
    inputs = dict(inputs or {})
    missing = []
    mismatched = []
    for port in _ports_of(spec):
        if port.name not in inputs:
            if port.required:
                missing.append(port.name)
            continue
        value = inputs[port.name]
        if port.link:
            ok = is_link_input(value)
        elif port.type == "any":
            ok = True
        elif port.type == "constant":
            ok = is_constant_input(value)
        else:
            ok = is_constant_input(value) and _type_matches(port.type, value)
        if not ok:
            mismatched.append(f"{port.name}(期望 {port.type})")
    if missing or mismatched:
        parts = []
        if missing:
            parts.append("缺少必填端口：" + ", ".join(missing))
        if mismatched:
            parts.append("端口类型不匹配：" + ", ".join(mismatched))
        raise PortValidationError("；".join(parts))
    return True
