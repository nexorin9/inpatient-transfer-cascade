"""图构建器：finalize / build 构建期校验，缺必填与端口类型不匹配在入队前被拒。

对应任务 14：构建期缺必填输入的图在入队前被拒并指出缺哪个端口。
"""
from __future__ import annotations

import pytest

from itc.graph.builder import GraphBuilder, GraphBuildError
from itc.graph.model import Graph, is_link
from itc.graph.registry import (
    NODE_TYPES,
    NodeType,
    get_node_type,
    register_node_type,
    unknown_type_fallback,
)


class TestBuilderBasics:
    def test_finalize_returns_serialized_nodes(self):
        b = GraphBuilder()
        b.node("fetch_orders", id="f1", inpatient_no="I")
        d = b.finalize()
        assert set(d) == {"f1"}
        assert d["f1"] == {
            "class_type": "fetch_orders",
            "inputs": {"inpatient_no": "I"},
        }

    def test_build_returns_graph(self):
        b = GraphBuilder()
        a = b.node("fetch_orders", id="a", inpatient_no="I")
        b.node("rule_judge", id="c", orders=a.out(0))
        g = b.build()
        assert isinstance(g, Graph)
        assert set(g.node_ids()) == {"a", "c"}
        assert g.downstream("a") == ["c"]
        assert is_link(g.nodes["c"].get_input("orders")) is True
        assert g.validate() is True

    def test_lookup_replace_remove(self):
        b = GraphBuilder(prefix="p.")
        a = b.node("fetch_orders", id="a", inpatient_no="I")
        c = b.node("rule_judge", id="c", orders=a.out(0), specimens=a.out(1))
        assert b.lookup_node("a").id == "p.a"
        b.replace_node_output("a", 0, ["ext", 3])
        assert c.get_input("orders") == ["ext", 3]
        assert c.get_input("specimens") == ["p.a", 1]
        b.replace_node_output("a", 1, None)
        assert c.get_input("specimens") is None
        b.remove_node("c")
        assert b.lookup_node("c") is None


class TestBuildTimeValidation:
    def test_missing_required_input_rejected(self):
        b = GraphBuilder()
        b.node("fetch_orders", id="a")  # 缺 inpatient_no
        with pytest.raises(GraphBuildError) as ei:
            b.finalize()
        assert "inpatient_no" in str(ei.value)
        with pytest.raises(GraphBuildError) as ei2:
            b.build()
        assert "inpatient_no" in str(ei2.value)

    def test_port_type_mismatch_rejected(self):
        b = GraphBuilder()
        src = b.node("fetch_orders", id="s", inpatient_no="I")
        # fetch_specimens 的 inpatient_no 期望常量字符串，却给了连线
        b.node("fetch_specimens", id="a", inpatient_no=src.out(0))
        with pytest.raises(GraphBuildError) as ei:
            b.finalize()
        assert "inpatient_no" in str(ei.value)
        assert "类型不匹配" in str(ei.value)

    def test_link_port_requires_link(self):
        b = GraphBuilder()
        b.node("rule_judge", id="a", orders="not-a-link")
        with pytest.raises(GraphBuildError) as ei:
            b.finalize()
        assert "orders" in str(ei.value)

    def test_dangling_link_rejected(self):
        b = GraphBuilder()
        b.node("rule_judge", id="a", orders=["ghost", 0])
        with pytest.raises(GraphBuildError) as ei:
            b.finalize()
        assert "ghost" in str(ei.value)

    def test_valid_graph_passes(self):
        b = GraphBuilder()
        a = b.node("fetch_orders", id="a", inpatient_no="I")
        b.node("rule_judge", id="c", orders=a.out(0))
        assert b.build().validate() is True


class TestRegistry:
    def test_node_types_complete(self):
        expected = {
            "fetch_orders", "fetch_specimens", "fetch_charges", "rule_judge",
            "model_judge", "change_site", "specimen_route", "bed_fee_hint",
            "stop_draft", "external_block",
        }
        assert expected <= set(NODE_TYPES)

    def test_get_and_fallback(self):
        assert get_node_type("fetch_orders").display_name
        assert get_node_type("no_such") is None
        fb = unknown_type_fallback("no_such")
        assert fb.writable is False
        assert fb.required_inputs == {}

    def test_register_custom(self):
        key = "custom_probe"
        try:
            register_node_type(
                NodeType(key, "自定义探针", {"x": "str"}, ("out",), False)
            )
            assert get_node_type(key).display_name == "自定义探针"
        finally:
            NODE_TYPES.pop(key, None)
