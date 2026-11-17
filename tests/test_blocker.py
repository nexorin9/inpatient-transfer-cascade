"""执行阻断语义：阻断节点自身及下游，携带原因并可序列化。"""
from __future__ import annotations

from itc.graph.blocker import ExecutionBlocker


class TestExecutionBlocker:
    def test_attributes(self):
        b = ExecutionBlocker("需医师判断", "judge", downstream={"a", "b"})
        assert b.message == "需医师判断"
        assert b.node_id == "judge"
        assert b.downstream == {"a", "b"}

    def test_blocks_self_and_downstream(self):
        b = ExecutionBlocker("m", "judge", downstream=["a", "b"])
        assert b.blocks("judge") is True
        assert b.blocks("a") is True
        assert b.blocks("b") is True
        assert b.blocks("other") is False

    def test_as_dict(self):
        b = ExecutionBlocker("m", "judge", downstream=["b", "a"])
        d = b.as_dict()
        assert set(d) == {"message", "node_id", "downstream"}
        assert d["message"] == "m"
        assert d["node_id"] == "judge"
        assert d["downstream"] == ["a", "b"]  # 稳定排序，便于落审计

    def test_empty_downstream(self):
        b = ExecutionBlocker("m", "judge")
        assert b.downstream == set()
        assert b.blocks("judge") is True
        assert b.blocks("a") is False
        assert b.as_dict()["downstream"] == []
