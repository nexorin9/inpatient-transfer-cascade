"""事件总线与进度事件：字段、状态集合、订阅 / 快照 / 取出清空、按序发出。"""
from __future__ import annotations

from itc.graph.events import EventBus, NODE_STATUS, ProgressEvent


def test_progress_event_fields_and_defaults():
    e = ProgressEvent("run-1", "node-a", "fetch_orders", NODE_STATUS["success"], "取数完成")
    assert e.run_id == "run-1"
    assert e.node_id == "node-a"
    assert e.node_type == "fetch_orders"
    assert e.status == "success"
    assert e.message == "取数完成"
    # ts 缺省自动补齐时间，且为 ASCII ISO 字符串
    assert e.ts and isinstance(e.ts, str)
    d = e.to_dict()
    assert d["node_id"] == "node-a" and d["status"] == "success"
    assert e.as_dict() == d


def test_node_status_contains_terminal_values():
    for key in ("success", "failed", "blocked", "cached", "running"):
        assert key in NODE_STATUS
        assert NODE_STATUS[key] == key


def test_event_bus_subscribe_emit_events():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda ev: seen.append(ev.node_id))
    e1 = ProgressEvent("r", "n1", "fetch", NODE_STATUS["running"])
    e2 = ProgressEvent("r", "n2", "judge", NODE_STATUS["success"])
    bus.emit(e1)
    bus.emit(e2)
    assert seen == ["n1", "n2"]
    assert [e.node_id for e in bus.events()] == ["n1", "n2"]


def test_event_bus_drain_and_clear():
    bus = EventBus()
    bus.emit(ProgressEvent("r", "n1"))
    bus.emit(ProgressEvent("r", "n2"))
    out = bus.drain()
    assert [e.node_id for e in out] == ["n1", "n2"]
    assert bus.events() == []
    bus.emit(ProgressEvent("r", "n3"))
    bus.clear()
    assert bus.events() == []


def test_event_bus_emits_in_node_order():
    bus = EventBus()
    order = ["A", "B", "C", "D"]
    for nid in order:
        bus.emit(ProgressEvent("r", nid, "judge", NODE_STATUS["success"]))
    assert [e.node_id for e in bus.events()] == order


def test_subscriber_error_is_recorded_not_silent():
    bus = EventBus()
    seen = []

    def bad(ev):
        raise RuntimeError("回调故障")

    bus.subscribe(bad)
    bus.subscribe(lambda ev: seen.append(ev.node_id))
    bus.emit(ProgressEvent("r", "n1"))
    # 坏回调不影响后续订阅者，也不静默吞掉
    assert seen == ["n1"]
    assert len(bus.callback_errors) == 1
    assert "回调故障" in bus.callback_errors[0]
