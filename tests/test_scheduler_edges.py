"""调度边界：环（含自环）、缺输入、重复连线去重、自环，四类边界各有用例。"""
from __future__ import annotations

import pytest

from itc.graph.model import Graph, Link, Node
from itc.graph.scheduler import (
    DependencyCycleError,
    NodeInputError,
    TopologicalSort,
    detect_cycle,
    ready_order,
    validate_required_inputs,
)


def _graph_with_self_loop():
    g = Graph()
    g.add_node(Node("a", "x", {"p": ["a", 0]}))
    g.add_link(Link("a", 0, "a", "p"))
    return g


class TestCycle:
    def test_detect_cycle_sequence_and_message(self):
        g = Graph()
        g.add_node(Node("a", "x", {"p": ["c", 0]}))
        g.add_node(Node("b", "x", {"p": ["a", 0]}))
        g.add_node(Node("c", "x", {"p": ["b", 0]}))
        for l in (Link("c", 0, "a", "p"), Link("a", 0, "b", "p"), Link("b", 0, "c", "p")):
            g.add_link(l)
        cycle = detect_cycle(g)
        assert cycle
        assert set(cycle) == {"a", "b", "c"}
        with pytest.raises(DependencyCycleError) as ei:
            ready_order(g)
        msg = str(ei.value)
        for nid in cycle:
            assert nid in msg

    def test_self_loop_detected(self):
        g = _graph_with_self_loop()
        assert detect_cycle(g) == ["a"]
        with pytest.raises(DependencyCycleError) as ei:
            ready_order(g)
        assert "a" in str(ei.value)

    def test_no_cycle_returns_empty(self):
        g = Graph()
        a = g.add_node(Node("a", "x", {}))
        g.add_node(Node("b", "x", {"p": a.out(0)}))
        g.add_link(Link("a", 0, "b", "p"))
        assert detect_cycle(g) == []
        assert ready_order(g) == ["a", "b"]


class TestMissingInput:
    def test_missing_required_port(self):
        g = Graph()
        g.add_node(Node("f", "fetch_orders", {}))
        with pytest.raises(NodeInputError) as ei:
            validate_required_inputs(g)
        assert "inpatient_no" in str(ei.value)

    def test_link_to_missing_node(self):
        g = Graph()
        g.add_node(Node("a", "rule_judge", {"orders": ["ghost", 0]}))
        ts = TopologicalSort(g)
        with pytest.raises(NodeInputError) as ei:
            ts.add_node("a")
        assert "ghost" in str(ei.value)


class TestDuplicateLinks:
    def test_add_link_dedupe(self):
        g = Graph()
        a = g.add_node(Node("a", "x", {}))
        g.add_node(Node("b", "x", {"p": a.out(0)}))
        for _ in range(3):
            g.add_link(Link("a", 0, "b", "p"))
        assert len(g.links) == 1

    def test_strong_link_no_double_block(self):
        g = Graph()
        a = g.add_node(Node("a", "x", {}))
        g.add_node(Node("b", "x", {"p": a.out(0)}))
        g.add_link(Link("a", 0, "b", "p"))
        ts = TopologicalSort(g)
        ts.add_node("b")
        assert ts.blockCount["b"] == 1
        ts.add_strong_link("a", 0, "b")
        ts.add_strong_link("a", 0, "b")
        assert ts.blockCount["b"] == 1  # 重复连线不产生两次传播
        ts.pop_node("a")
        assert ts.get_ready_nodes() == ["b"]

    def test_ready_order_no_duplicates(self):
        g = Graph()
        a = g.add_node(Node("a", "x", {}))
        g.add_node(Node("b", "x", {"p": a.out(0)}))
        for _ in range(3):
            g.add_link(Link("a", 0, "b", "p"))
        order = ready_order(g)
        assert order.count("a") == 1 and order.count("b") == 1
        assert order == ["a", "b"]
