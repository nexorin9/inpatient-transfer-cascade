"""执行阻断语义：阻断节点自身及其下游，并携带原因。

对齐源机制：ComfyUI comfy_execution/graph_utils.py:140 ExecutionBlocker(message)。
"""
from __future__ import annotations


class ExecutionBlocker:
    def __init__(self, message, node_id, downstream=None):
        self.message = message
        self.node_id = node_id
        self.downstream = set(downstream or [])

    def blocks(self, node_id):
        """被阻断的节点 = 阻断节点自身 + 其下游节点。"""
        return node_id == self.node_id or node_id in self.downstream

    def as_dict(self):
        return {
            "message": self.message,
            "node_id": self.node_id,
            "downstream": sorted(self.downstream),
        }

    def __repr__(self):
        return f"ExecutionBlocker(node_id={self.node_id!r}, message={self.message!r})"
