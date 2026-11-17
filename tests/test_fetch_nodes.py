"""任务 39：取数节点接线、解析失败 / 空结果区分、局部重跑计数。"""
from __future__ import annotations

import pytest

from itc.domain.models import BedCharge, ExecutingOrder, Specimen
from itc.graph.fetch_nodes import (
    FetchCache,
    FetchNodeError,
    counting_reader,
    downstream_nodes,
    execute_fetch_node,
    fetch_cache_key,
    make_fetch_node,
)


def _order_rows(n):
    return [
        {
            "order_no": f"O-{i}", "content": "c", "drug_name": "d", "route": "r",
            "frequency": "f", "execute_dept_code": "DEPT", "start_at": "t",
        }
        for i in range(n)
    ]


def _specimen_rows(n):
    return [
        {
            "specimen_no": f"S-{i}", "test_name": "test", "collect_dept_code": "DEPT",
            "status": "pending",
        }
        for i in range(n)
    ]


def _charge_rows(n):
    return [
        {
            "category": "bed", "fee_date": "2026-01-01", "amount": "10.00",
            "dept_code": "DEPT",
        }
        for i in range(n)
    ]


class _FakeReader:
    """测试替身：方法名与 H 组 reader 契约一致，只接 inpatient_no。"""

    def __init__(self, orders=None, specimens=None, charges=None):
        self._orders = list(orders or [])
        self._specimens = list(specimens or [])
        self._charges = list(charges or [])

    def read_executing_orders(self, inpatient_no):
        return list(self._orders)

    def read_pending_specimens(self, inpatient_no):
        return list(self._specimens)

    def read_bed_charges(self, inpatient_no):
        return list(self._charges)


def test_make_fetch_node_contains_inpatient_no_and_filters():
    node = make_fetch_node("orders", "10001", filters={"status": "pending"})
    assert node["inputs"]["inpatient_no"] == "10001"
    assert node["inputs"]["filters"] == {"status": "pending"}
    assert node["resource_type"] == "orders"
    assert node["class_type"] == "fetch_orders"
    assert node["cache_key"].startswith("fetch:")
    assert node["downstream"] == ["judge_orders"]


def test_make_fetch_node_class_types_for_all_resources():
    assert make_fetch_node("orders", "1")["class_type"] == "fetch_orders"
    assert make_fetch_node("specimens", "1")["class_type"] == "fetch_specimens"
    assert make_fetch_node("charges", "1")["class_type"] == "fetch_charges"


def test_make_fetch_node_rejects_unknown_resource():
    with pytest.raises(ValueError):
        make_fetch_node("unknown", "1")


def test_execute_fetch_node_row_count_matches_reader():
    rows = _order_rows(4)
    result = execute_fetch_node(make_fetch_node("orders", "10001"), _FakeReader(orders=rows))
    assert len(result) == len(rows) == 4
    assert all(isinstance(item, ExecutingOrder) for item in result)


def test_execute_calls_correct_reader_method_per_resource():
    reader = counting_reader(_FakeReader(
        orders=_order_rows(2), specimens=_specimen_rows(3), charges=_charge_rows(1)
    ))
    orders = execute_fetch_node(make_fetch_node("orders", "1"), reader)
    specimens = execute_fetch_node(make_fetch_node("specimens", "1"), reader)
    charges = execute_fetch_node(make_fetch_node("charges", "1"), reader)

    assert len(orders) == 2 and all(isinstance(o, ExecutingOrder) for o in orders)
    assert len(specimens) == 3 and all(isinstance(s, Specimen) for s in specimens)
    assert len(charges) == 1 and all(isinstance(c, BedCharge) for c in charges)
    assert reader.counts["read_executing_orders"] == 1
    assert reader.counts["read_pending_specimens"] == 1
    assert reader.counts["read_bed_charges"] == 1
    assert reader.call_count() == 3


def test_filters_applied_to_reader_output():
    rows = _order_rows(3)
    rows[0]["execute_dept_code"] = "DEPT-A"
    rows[1]["execute_dept_code"] = "DEPT-B"
    rows[2]["execute_dept_code"] = "DEPT-A"
    node = make_fetch_node("orders", "1", filters={"execute_dept_code": "DEPT-A"})
    result = execute_fetch_node(node, _FakeReader(orders=rows))
    assert len(result) == 2
    assert all(o.execute_dept_code == "DEPT-A" for o in result)


def test_parse_failure_raises_fetch_node_error_and_blocks_downstream():
    reader = _FakeReader(orders=[{"order_no": "O-1"}])  # 缺必填字段
    node = make_fetch_node("orders", "1")
    with pytest.raises(FetchNodeError) as exc_info:
        execute_fetch_node(node, reader)
    err = exc_info.value
    assert err.node_id == node["id"]
    assert err.blocks("judge_orders") is True
    assert "judge_orders" in err.downstream
    assert err.reason and "解析失败" in err.reason


def test_non_list_reader_result_is_parse_failure():
    class _BadReader(_FakeReader):
        def read_executing_orders(self, inpatient_no):
            return {"error": "oops"}

    with pytest.raises(FetchNodeError):
        execute_fetch_node(make_fetch_node("orders", "1"), _BadReader())


def test_reader_exception_propagates_for_retry_layer():
    class _BoomReader(_FakeReader):
        def read_executing_orders(self, inpatient_no):
            raise ConnectionError("HIS 不可达")

    # reader 侧的可重试错误原样抛出，交给节点级重试/熔断层分类
    with pytest.raises(ConnectionError):
        execute_fetch_node(make_fetch_node("orders", "1"), _BoomReader())


def test_empty_result_is_success_and_downstream_does_not_grow():
    reader = counting_reader(_FakeReader(orders=[]))
    node = make_fetch_node("orders", "1")
    result = execute_fetch_node(node, reader)
    assert result == []
    assert downstream_nodes(result, node) == []
    assert reader.call_count() == 1  # 节点确实执行了取数，返回空视为成功

    nonempty = execute_fetch_node(make_fetch_node("orders", "2"), _FakeReader(orders=_order_rows(2)))
    assert downstream_nodes(nonempty, node) == ["judge_orders"]


def test_missing_inpatient_no_is_fetch_node_error():
    node = make_fetch_node("orders", "1")
    node["inputs"]["inpatient_no"] = ""
    with pytest.raises(FetchNodeError):
        execute_fetch_node(node, _FakeReader(orders=_order_rows(1)))


def test_counting_reader_counts_per_method():
    wrapped = counting_reader(_FakeReader(orders=_order_rows(1)))
    wrapped.read_executing_orders("1")
    wrapped.read_executing_orders("1")
    wrapped.read_pending_specimens("1")
    assert wrapped.counts["read_executing_orders"] == 2
    assert wrapped.counts["read_pending_specimens"] == 1
    assert wrapped.call_count() == 3
    assert wrapped.call_count("read_executing_orders") == 2
    assert len(wrapped.calls) == 3


def test_fetch_cache_key_stable_and_field_sensitive():
    assert fetch_cache_key("orders", "1", {"a": 1}) == fetch_cache_key("orders", "1", {"a": 1})
    assert fetch_cache_key("orders", "1", {"a": 1}) != fetch_cache_key("orders", "1", {"a": 2})
    assert fetch_cache_key("orders", "1") != fetch_cache_key("orders", "2")


def test_partial_rerun_hits_cache_no_extra_fetch():
    cache = FetchCache()
    reader = counting_reader(_FakeReader(orders=_order_rows(5)))
    node = make_fetch_node("orders", "10001")

    first = cache.get_or_execute(node, reader)
    second = cache.get_or_execute(node, reader)  # 同一节点局部重跑
    assert len(first) == 5 and len(second) == 5
    assert reader.call_count() == 1  # 重复跑取数调用次数不增加
    assert cache.summary()["hits"] == 1
    assert cache.summary()["misses"] == 1

    # 业务字段变化 → 缓存键不同 → 重新取数
    cache.get_or_execute(make_fetch_node("orders", "10002"), reader)
    assert reader.call_count() == 2
    assert cache.summary()["misses"] == 2
