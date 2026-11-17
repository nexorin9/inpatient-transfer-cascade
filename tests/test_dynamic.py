"""运行时动态插节点：父链回溯、按患者数据决定节点集合、重复触发去重、乱序插入。

对应任务 3 验收：同一转科单两种患者数据节点集合不同、父链回溯真实 id 正确。
"""
from __future__ import annotations

import pytest

from itc.graph.dynamic import (
    DynamicPrompt,
    OutOfOrderInsertionError,
    decide_nodes,
    insert_disposition_nodes,
)
from itc.graph.scheduler import NodeNotFoundError


def _root_prompt():
    return {
        "fetch_orders": {"class_type": "fetch_orders", "inputs": {"inpatient_no": "I"}},
        "judge": {"class_type": "rule_judge", "inputs": {"orders": ["fetch_orders", 0]}},
    }


def _info(class_type, parent):
    return {"class_type": class_type, "inputs": {"source": [parent, 0]}}


class TestDynamicPrompt:
    def test_add_and_get_node(self):
        d = DynamicPrompt(_root_prompt())
        d.add_ephemeral_node(
            "e1", _info("change_site", "judge"), "judge", display_id="e1"
        )
        assert d.has_node("e1") and d.has_node("fetch_orders")
        assert d.get_node("e1")["class_type"] == "change_site"
        assert "e1" in d.all_node_ids()
        assert d.get_parent_node_id("e1") == "judge"
        assert d.get_parent_node_id("judge") is None
        with pytest.raises(NodeNotFoundError):
            d.get_node("missing")

    def test_real_node_id_single_level(self):
        d = DynamicPrompt(_root_prompt())
        d.add_ephemeral_node("e1", _info("change_site", "fetch_orders"), "fetch_orders")
        assert d.get_real_node_id("e1") == "fetch_orders"
        assert d.get_real_node_id("fetch_orders") == "fetch_orders"

    def test_real_node_id_multi_level(self):
        d = DynamicPrompt(_root_prompt())
        d.add_ephemeral_node("e1", _info("change_site", "fetch_orders"), "fetch_orders")
        d.add_ephemeral_node("e2", _info("specimen_route", "e1"), "e1")
        d.add_ephemeral_node("e3", _info("bed_fee_hint", "e2"), "e2")
        assert d.get_real_node_id("e1") == "fetch_orders"
        assert d.get_real_node_id("e2") == "fetch_orders"
        assert d.get_real_node_id("e3") == "fetch_orders"

    def test_display_node_id_chain(self):
        d = DynamicPrompt(_root_prompt())
        d.add_ephemeral_node(
            "e1", _info("change_site", "fetch_orders"), "fetch_orders",
            display_id="fetch_orders",
        )
        d.add_ephemeral_node(
            "e2", _info("specimen_route", "e1"), "e1", display_id="e1"
        )
        assert d.get_display_node_id("e2") == "fetch_orders"
        assert d.get_display_node_id("judge") == "judge"

    def test_out_of_order_insertion(self):
        d = DynamicPrompt(_root_prompt())
        with pytest.raises(OutOfOrderInsertionError) as ei:
            d.add_ephemeral_node(
                "e1", _info("change_site", "fetch_orders"), "fetch_orders",
                completed_parents=set(),
            )
        assert "fetch_orders" in str(ei.value)
        d.add_ephemeral_node(
            "e1", _info("change_site", "fetch_orders"), "fetch_orders",
            completed_parents={"fetch_orders"},
        )
        assert d.has_node("e1")


class TestDecideNodes:
    def test_no_executing_orders_no_change_site(self):
        nodes = decide_nodes(
            {"executing_orders": [], "pending_specimens": [{"specimen_no": "S"}], "charges": []}
        )
        assert "change_site" not in nodes
        assert "specimen_route" in nodes

    def test_pending_specimens_adds_specimen_route(self):
        nodes = decide_nodes(
            {"executing_orders": [{"order_no": "O"}], "pending_specimens": [], "charges": []}
        )
        assert "change_site" in nodes
        assert "specimen_route" not in nodes

    def test_two_patient_data_differ(self):
        a = decide_nodes(
            {
                "executing_orders": [{"order_no": "O"}],
                "pending_specimens": [{"specimen_no": "S"}],
                "charges": [{"fee_date": "D"}],
            }
        )
        b = decide_nodes(
            {"executing_orders": [], "pending_specimens": [], "charges": []}
        )
        assert set(a) != set(b)
        assert set(a) == {"change_site", "specimen_route", "bed_fee_hint"}
        assert b == []


class TestInsertDispositionNodes:
    def test_dedupe_on_repeated_trigger(self):
        d = DynamicPrompt(_root_prompt())
        data = {
            "executing_orders": [{"order_no": "O"}],
            "pending_specimens": [{"specimen_no": "S"}],
            "charges": [{"fee_date": "D"}],
        }
        first = insert_disposition_nodes(d, data, {"fetch_orders"})
        assert len(first) == 3
        before = set(d.all_node_ids())
        second = insert_disposition_nodes(d, data, {"fetch_orders"})
        assert second == []
        assert set(d.all_node_ids()) == before

    def test_two_patient_data_node_sets_differ(self):
        base = _root_prompt()
        d1 = DynamicPrompt(base)
        d2 = DynamicPrompt(base)
        insert_disposition_nodes(
            d1, {"executing_orders": [{"order_no": "O"}], "pending_specimens": [], "charges": []},
            {"fetch_orders"},
        )
        insert_disposition_nodes(
            d2, {"executing_orders": [], "pending_specimens": [{"specimen_no": "S"}], "charges": []},
            {"fetch_orders"},
        )
        assert d1.all_node_ids() != d2.all_node_ids()
        assert any("change_site" in n for n in d1.all_node_ids())
        assert any("specimen_route" in n for n in d2.all_node_ids())

    def test_parent_not_completed_raises(self):
        d = DynamicPrompt(_root_prompt())
        with pytest.raises(OutOfOrderInsertionError) as ei:
            insert_disposition_nodes(
                d, {"executing_orders": [{"order_no": "O"}]}, set()
            )
        assert "fetch_orders" in str(ei.value)
