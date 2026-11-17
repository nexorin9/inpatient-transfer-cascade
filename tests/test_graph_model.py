"""图模型：is_link 精确区分、Node.serialize 形态、序列化往返与图结构等价。

同时覆盖 G1 的幂等键与 scope 主干最窄端到端切片（本组没有单独的测试文件）。
"""
from __future__ import annotations

import json

import pytest

from itc.graph.idempotency import business_hash, idempotency_key
from itc.graph.model import Graph, GraphValidationError, Link, Node, is_link
from itc.graph.scope import build_scope_list, write_scope_list


class TestIsLink:
    def test_link_inputs(self):
        assert is_link(["n1", 0]) is True
        assert is_link(["n1", 1]) is True
        assert is_link(["n1", 0.0]) is True
        assert is_link(["fetch_orders", 2]) is True

    def test_constant_inputs_are_not_links(self):
        assert is_link("<INPATIENT_NO>") is False
        assert is_link(1) is False
        assert is_link(1.5) is False
        assert is_link(None) is False
        assert is_link({"a": 1}) is False
        assert is_link(["n1"]) is False
        assert is_link(["n1", 0, 1]) is False
        assert is_link([1, 0]) is False
        assert is_link(["n1", "0"]) is False
        assert is_link(["n1", None]) is False


class TestNodeSerialization:
    def test_serialize_shape(self):
        n = Node("n1", "fetch_orders", {"inpatient_no": "<INPATIENT_NO>"})
        s = n.serialize()
        assert set(s) == {"class_type", "inputs"}
        assert s["class_type"] == "fetch_orders"
        assert s["inputs"] == {"inpatient_no": "<INPATIENT_NO>"}

    def test_override_display_id_included(self):
        n = Node("n1", "x", {}, override_display_id="disp")
        assert n.serialize()["override_display_id"] == "disp"

    def test_roundtrip_node(self):
        n = Node(
            "n2",
            "change_site",
            {"source": ["n1", 0], "order_no": "<O>"},
            override_display_id="d2",
        )
        restored = Node.deserialize("n2", n.serialize())
        assert restored.id == n.id
        assert restored.class_type == n.class_type
        assert restored.inputs == n.inputs
        assert restored.override_display_id == n.override_display_id
        assert restored.serialize() == n.serialize()

    def test_out_is_link(self):
        n = Node("n1", "fetch_orders")
        assert is_link(n.out(0)) is True
        assert n.out(0) == ["n1", 0]

    def test_set_get_input(self):
        n = Node("n1", "x")
        n.set_input("a", ["n0", 1])
        assert n.get_input("a") == ["n0", 1]
        n.set_input("a", None)
        assert n.get_input("a") is None


class TestGraphRoundtrip:
    def _graph(self):
        g = Graph()
        a = g.add_node(Node("a", "fetch_orders", {"inpatient_no": "I"}))
        b = g.add_node(Node("b", "rule_judge", {"orders": a.out(0)}))
        c = g.add_node(Node("c", "change_site", {"source": b.out(0), "order_no": "<O>"}))
        g.add_link(Link("a", 0, "b", "orders"))
        g.add_link(Link("b", 0, "c", "source"))
        return g

    def test_to_dict_shape(self):
        d = self._graph().to_dict()
        assert set(d) == {"nodes", "links"}
        assert set(d["nodes"]) == {"a", "b", "c"}
        assert all(set(v) >= {"class_type", "inputs"} for v in d["nodes"].values())

    def test_roundtrip_structure_equal(self):
        g = self._graph()
        restored = Graph.from_dict(json.loads(json.dumps(g.to_dict(), ensure_ascii=False)))
        assert restored.to_dict() == g.to_dict()
        assert restored.node_ids() == g.node_ids()
        assert restored.downstream("a") == g.downstream("a")
        assert restored.downstream("b") == g.downstream("b")
        assert restored.validate() is True

    def test_downstream_and_dedupe(self):
        g = self._graph()
        assert g.downstream("a") == ["b"]
        g.add_link(Link("a", 0, "b", "orders"))
        assert len(g.links) == 2
        assert [l for l in g.links if l.from_node_id == "a"] == [Link("a", 0, "b", "orders")]
        assert g.downstream("a") == ["b"]

    def test_validate_rejects_dangling_link(self):
        g = Graph()
        g.add_node(Node("a", "x", {"src": ["missing", 0]}))
        with pytest.raises(GraphValidationError):
            g.validate()


class TestIdempotency:
    def test_same_business_fields_equal(self):
        a = idempotency_key("<T>", "<O>", "<D>", "2026-01-01T09:00:00+08:00")
        b = idempotency_key("<T>", "<O>", "<D>", "2026-01-01T09:00:00+08:00")
        assert a == b
        assert len(a) == 64
        assert all(ch in "0123456789abcdef" for ch in a)

    def test_effective_from_changes_key(self):
        a = idempotency_key("<T>", "<O>", "<D>", "2026-01-01T09:00:00+08:00")
        b = idempotency_key("<T>", "<O>", "<D>", "2026-01-02T09:00:00+08:00")
        assert a != b

    def test_other_business_fields_change_key(self):
        base = idempotency_key("<T>", "<O>", "<D>", "E")
        assert base != idempotency_key("<T2>", "<O>", "<D>", "E")
        assert base != idempotency_key("<T>", "<O2>", "<D>", "E")
        assert base != idempotency_key("<T>", "<O>", "<D2>", "E")

    def test_independent_of_timestamp_and_retries(self):
        # idempotency_key 只由四个业务字段决定，签名里没有时间戳 / 重试次数
        assert idempotency_key("<T>", "<O>", "<D>", "E") == idempotency_key(
            "<T>", "<O>", "<D>", "E"
        )
        h1 = business_hash(transfer_no="<T>", order_no="<O>", timestamp="t1", retries=0)
        h2 = business_hash(transfer_no="<T>", order_no="<O>", timestamp="t2", retries=9)
        h3 = business_hash(transfer_no="<T>", order_no="<O2>", timestamp="t1", retries=0)
        assert h1 == h2
        assert h1 != h3


class TestScopeSlice:
    def _load(self, fixtures_dir, name):
        return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))

    def _samples(self, fixtures_dir):
        transfer = self._load(fixtures_dir, "transfer_sample.json")
        orders = self._load(fixtures_dir, "orders_sample.json")["items"]
        specimens = self._load(fixtures_dir, "specimens_sample.json")["items"]
        charges = self._load(fixtures_dir, "charges_sample.json")["items"]
        return transfer, orders, specimens, charges

    def test_end_to_end_slice(self, fixtures_dir, tmp_path):
        transfer, orders, specimens, charges = self._samples(fixtures_dir)
        scope = build_scope_list(transfer, orders, specimens, charges)

        assert "transfer_no" in scope and scope["transfer_no"] == transfer["transfer_no"]
        assert "inpatient_no" in scope
        assert "items" in scope
        items = scope["items"]
        assert len(items) == 4  # 2 条医嘱 + 1 张待送标本 + 1 条床位费
        for item in items:
            assert set(item) >= {"kind", "ref", "action", "note"}
            assert item["action"] and item["note"]
        kinds = [i["kind"] for i in items]
        assert kinds.count("order") == 2
        assert kinds.count("specimen") == 1
        assert kinds.count("charge") == 1

        # 就绪调度确实跑过：拓扑序含全部节点、无重复、判定节点在取数节点之后
        node_order = scope["node_order"]
        assert node_order and len(node_order) == len(set(node_order))
        assert "judge" in node_order
        assert all(
            node_order.index("judge") > node_order.index(f)
            for f in ("fetch_orders", "fetch_specimens", "fetch_charges")
        )

        # 产物写入函数可用（绝对路径）
        out = write_scope_list(scope, tmp_path / "outputs" / "scope.json")
        assert out.exists()
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["transfer_no"] == transfer["transfer_no"]
        assert len(written["items"]) == 4
        assert all("kind" in i and "ref" in i for i in written["items"])

    def test_relative_path_written_to_outputs(self, fixtures_dir):
        transfer, orders, _, _ = self._samples(fixtures_dir)
        scope = build_scope_list(transfer, orders, [], [])
        out = write_scope_list(scope, "scope_selfcheck.json")
        assert out.parent.name == "outputs"
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "transfer_no" in data and "items" in data
