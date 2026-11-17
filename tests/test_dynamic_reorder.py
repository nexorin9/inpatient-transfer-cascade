"""动态插节点后的依赖重排与父链回溯边界。

对应任务 16：既有相对顺序不变、两种患者数据节点集合不同、重复触发不重复插入、父链唯一。
"""
from __future__ import annotations

import pytest

from itc.graph.dynamic import (
    DynamicPrompt,
    OutOfOrderInsertionError,
    insert_disposition_nodes,
    reorder_after_insert,
)
from itc.graph.scheduler import ready_order


def _prompt_with_existing():
    return {
        "fetch_orders": {"class_type": "fetch_orders", "inputs": {"inpatient_no": "I"}},
        "judge": {"class_type": "rule_judge", "inputs": {"orders": ["fetch_orders", 0]}},
        "existing_a": {"class_type": "change_site", "inputs": {"source": ["judge", 0]}},
        "existing_b": {"class_type": "bed_fee_hint", "inputs": {"source": ["judge", 0]}},
    }


class TestReorder:
    def test_existing_relative_order_preserved(self):
        base = _prompt_with_existing()
        before = ready_order(base)  # 插入前的既有稳定拓扑序
        d = DynamicPrompt(base)
        new_ids = insert_disposition_nodes(
            d,
            {
                "executing_orders": [{"order_no": "N"}],
                "pending_specimens": [{"specimen_no": "S"}],
            },
            {"fetch_orders", "judge"},
        )
        assert new_ids
        order = reorder_after_insert(d, new_ids)
        existing_after = [n for n in order if n not in set(new_ids)]
        assert existing_after == before
        assert len(order) == len(set(order))
        for nid in new_ids:
            assert order.index(nid) > order.index("fetch_orders")

    def test_reorder_is_valid_topological_order(self):
        base = _prompt_with_existing()
        d = DynamicPrompt(base)
        new_ids = insert_disposition_nodes(
            d, {"pending_specimens": [{"specimen_no": "S"}]}, {"fetch_orders", "judge"}
        )
        order = reorder_after_insert(d, new_ids)
        assert set(order) == d.all_node_ids()
        assert order.index("judge") > order.index("fetch_orders")
        assert len(order) == len(set(order))

    def test_repeated_trigger_no_duplicate_nodes(self):
        base = _prompt_with_existing()
        d = DynamicPrompt(base)
        data = {"executing_orders": [{"order_no": "O"}]}
        first = insert_disposition_nodes(d, data, {"fetch_orders", "judge"})
        second = insert_disposition_nodes(d, data, {"fetch_orders", "judge"})
        assert second == []
        order = reorder_after_insert(d, first)
        assert len(order) == len(set(order))

    def test_two_patient_data_node_sets_differ(self):
        base = _prompt_with_existing()
        d1 = DynamicPrompt(base)
        d2 = DynamicPrompt(base)
        insert_disposition_nodes(
            d1,
            {"executing_orders": [{"order_no": "O"}], "pending_specimens": [], "charges": []},
            {"fetch_orders", "judge"},
        )
        insert_disposition_nodes(
            d2,
            {"executing_orders": [], "pending_specimens": [{"specimen_no": "S"}], "charges": []},
            {"fetch_orders", "judge"},
        )
        assert d1.all_node_ids() != d2.all_node_ids()

    def test_multi_layer_parent_chain_unique(self):
        base = _prompt_with_existing()
        d = DynamicPrompt(base)
        d.add_ephemeral_node(
            "e1", {"class_type": "change_site", "inputs": {"source": ["judge", 0]}}, "judge"
        )
        d.add_ephemeral_node(
            "e2", {"class_type": "specimen_route", "inputs": {"source": ["e1", 0]}}, "e1"
        )
        d.add_ephemeral_node(
            "e3", {"class_type": "bed_fee_hint", "inputs": {"source": ["e2", 0]}}, "e2"
        )
        # 动态节点插到动态节点之下，真实节点 id 仍唯一确定
        assert d.get_real_node_id("e1") == "judge"
        assert d.get_real_node_id("e2") == "judge"
        assert d.get_real_node_id("e3") == "judge"
        order = reorder_after_insert(d, ["e1", "e2", "e3"])
        assert order.index("e1") < order.index("e2") < order.index("e3")
        assert len(order) == len(set(order))

    def test_out_of_order_insertion_mentions_parent(self):
        base = _prompt_with_existing()
        d = DynamicPrompt(base)
        with pytest.raises(OutOfOrderInsertionError) as ei:
            insert_disposition_nodes(
                d, {"executing_orders": [{"order_no": "O"}]}, set()
            )
        assert "fetch_orders" in str(ei.value)
