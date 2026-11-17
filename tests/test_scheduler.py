"""就绪调度：挡数计数、稳定拓扑序、外部阻塞、必填输入校验。"""
from __future__ import annotations

import pytest

from itc.graph.model import Graph, Link, Node
from itc.graph.scheduler import (
    DependencyCycleError,
    NodeInputError,
    TopologicalSort,
    ready_order,
    validate_required_inputs,
)


def _chain():
    g = Graph()
    a = g.add_node(Node("a", "fetch_orders", {"inpatient_no": "I"}))
    b = g.add_node(Node("b", "rule_judge", {"orders": a.out(0)}))
    c = g.add_node(Node("c", "change_site", {"source": b.out(0)}))
    g.add_link(Link("a", 0, "b", "orders"))
    g.add_link(Link("b", 0, "c", "source"))
    return g


class TestTopologicalSort:
    def test_block_count_and_ready_nodes(self):
        ts = TopologicalSort(_chain())
        ts.add_node("c")
        assert ts.blockCount == {"c": 1, "b": 1, "a": 0}
        assert ts.get_ready_nodes() == ["a"]
        ts.pop_node("a")
        assert ts.get_ready_nodes() == ["b"]
        ts.pop_node("b")
        assert ts.get_ready_nodes() == ["c"]
        ts.pop_node("c")
        assert ts.is_empty() is True

    def test_external_block_and_cached(self):
        ts = TopologicalSort(_chain())
        ts.add_node("b")
        assert ts.is_cached("a") is False
        unblock = ts.add_external_block("b")
        assert ts.get_ready_nodes() == ["a"]
        ts.pop_node("a")
        assert ts.get_ready_nodes() == []  # b 仍被外部阻塞
        assert unblock() is True
        assert ts.get_ready_nodes() == ["b"]

    def test_cached_upstream_not_pending(self):
        ts = TopologicalSort(_chain())
        ts._cached.add("a")
        assert ts.is_cached("a") is True
        ts.add_node("c")
        # a 被视作已缓存，不加入待执行集合，b 直接就绪
        assert "a" not in ts.pendingNodes
        assert ts.get_ready_nodes() == ["b"]


class TestReadyOrder:
    def test_stable_topological_order(self):
        g = _chain()
        order = ready_order(g)
        assert order == ["a", "b", "c"]
        assert ready_order(g) == order
        assert len(order) == len(set(order)) == 3
        assert order.index("a") < order.index("b") < order.index("c")

    def test_fanout_is_deterministic(self):
        g = Graph()
        root = g.add_node(Node("root", "fetch_orders", {"inpatient_no": "I"}))
        for name in ("x", "y", "z"):
            g.add_node(Node(name, "change_site", {"source": root.out(0)}))
            g.add_link(Link("root", 0, name, "source"))
        order = ready_order(g)
        assert order == ["root", "x", "y", "z"]
        assert set(order) == set(g.node_ids())

    def test_cycle_raises_with_node_sequence(self):
        g = Graph()
        g.add_node(Node("a", "x", {"p": ["b", 0]}))
        g.add_node(Node("b", "x", {"p": ["a", 0]}))
        g.add_link(Link("b", 0, "a", "p"))
        g.add_link(Link("a", 0, "b", "p"))
        with pytest.raises(DependencyCycleError) as ei:
            ready_order(g)
        assert "a" in str(ei.value) and "b" in str(ei.value)


class TestValidateRequiredInputs:
    def test_valid_graph(self):
        assert validate_required_inputs(_chain()) is True

    def test_missing_port_is_named(self):
        g = Graph()
        g.add_node(Node("n1", "fetch_orders", {}))  # 缺 inpatient_no
        with pytest.raises(NodeInputError) as ei:
            validate_required_inputs(g)
        assert "inpatient_no" in str(ei.value)
        assert "n1" in str(ei.value)
