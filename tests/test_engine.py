"""执行主循环：入队校验、后台工作线程、进度顺序、三种终态、重复注册。"""
from __future__ import annotations

import time

from itc.graph.engine import Engine
from itc.graph.events import EventBus, NODE_STATUS


def _linear_graph():
    return {
        "A": {"class_type": "fetch_orders", "inputs": {}},
        "B": {"class_type": "rule_judge", "inputs": {"orders": ["A", 0]}},
        "C": {"class_type": "change_site", "inputs": {"decision": ["B", 0]}},
    }


def _ok_executor(node_id, info, run_id, transfer_no):
    return {"status": "ok", "output": {"n": node_id}, "summary": "ok-" + node_id,
            "source": info.get("source", "")}


def test_submit_validates_and_rejects_with_readable_reason(db_conn):
    e = Engine(db_conn)
    # 图结构非法：空图
    assert e.submit("v0", {}) is False
    assert "图结构非法" in e.rejection_reason("v0")
    # 缺必填输入：连线引用不存在的上游节点
    assert e.submit("v1", {"A": {"inputs": {"x": ["GHOST", 0]}}}) is False
    assert "缺必填输入" in e.rejection_reason("v1")
    # 图结构非法：依赖环
    cyc = {"A": {"inputs": {"x": ["B", 0]}}, "B": {"inputs": {"x": ["A", 0]}}}
    assert e.submit("v2", cyc) is False
    assert "环" in e.rejection_reason("v2")
    # 模板版本不识别
    e_bad = Engine(db_conn, template_version="999")
    assert e_bad.submit("v3", {"A": {"inputs": {}}}) is False
    assert "模板版本" in e_bad.rejection_reason("v3")


def test_duplicate_registration_ignored(db_conn):
    e = Engine(db_conn)
    g = {"A": {"inputs": {}}}
    assert e.submit("dup", g) is True
    assert e.submit("dup", g) is False
    assert "重复注册" in e.rejection_reason("dup")
    rows = db_conn.execute(
        "SELECT COUNT(*) FROM graph_runs WHERE run_id='dup'"
    ).fetchone()[0]
    assert rows == 1


def test_progress_events_emitted_in_node_order(db_conn):
    bus = EventBus()
    e = Engine(db_conn, bus=bus, executor=_ok_executor)
    assert e.submit("p1", _linear_graph()) is True
    e.run("p1")
    events = bus.events()
    assert [ev.node_id for ev in events] == ["A", "B", "C"]
    assert all(ev.run_id == "p1" for ev in events)
    assert all(ev.status == NODE_STATUS["success"] for ev in events)


def test_three_terminal_states_land_in_graph_runs(db_conn):
    g = {
        "A": {"class_type": "fetch", "inputs": {}},
        "B": {"class_type": "judge", "inputs": {"x": ["A", 0]}},
    }
    # 三个 run 用不同的 transfer_no：业务哈希不同，避免命中上一 run 的落盘缓存，
    # 确保各 executor 真的执行到（失败 / 阻断终态才能被触发）。
    # 成功
    e_ok = Engine(db_conn, executor=_ok_executor)
    assert e_ok.submit("ok-run", g, transfer_no="T-ok") is True
    assert e_ok.run("ok-run").status == "success"
    # 失败（执行器抛异常，不吞异常）
    def boom(node_id, info, run_id, transfer_no):
        raise RuntimeError("HIS 取数失败")

    e_fail = Engine(db_conn, executor=boom)
    assert e_fail.submit("fail-run", g, transfer_no="T-fail") is True
    rf = e_fail.run("fail-run")
    assert rf.status == "failed"
    assert rf.errors and rf.errors[0]["error"] == "HIS 取数失败"
    assert "traceback" in rf.errors[0]
    # 阻断（执行器返回 blocked）
    def blk(node_id, info, run_id, transfer_no):
        if node_id == "B":
            return {"status": "blocked", "summary": "需人工确认"}
        return {"status": "ok", "summary": "ok", "source": ""}

    e_blk = Engine(db_conn, executor=blk)
    assert e_blk.submit("block-run", g, transfer_no="T-block") is True
    assert e_blk.run("block-run").status == "blocked"

    statuses = {}
    for rid in ("ok-run", "fail-run", "block-run"):
        statuses[rid] = db_conn.execute(
            "SELECT status FROM graph_runs WHERE run_id=?", (rid,)
        ).fetchone()[0]
    assert statuses == {
        "ok-run": "success", "fail-run": "failed", "block-run": "blocked",
    }
    # 失败异常也落台账摘要，可见未吞
    s = db_conn.execute(
        "SELECT summary_json FROM graph_runs WHERE run_id='fail-run'"
    ).fetchone()[0]
    assert "HIS 取数失败" in s


def test_worker_executes_in_background_without_blocking_caller(db_conn):
    def slow(node_id, info, run_id, transfer_no):
        time.sleep(0.05)
        return {"status": "ok", "summary": "ok", "source": ""}

    g = {
        "X": {"inputs": {}},
        "Y": {"inputs": {"x": ["X", 0]}},
        "Z": {"inputs": {"x": ["Y", 0]}},
    }
    e = Engine(db_conn, executor=slow)
    e.start_worker()
    try:
        t0 = time.time()
        assert e.submit("w1", g) is True
        elapsed = time.time() - t0
        assert elapsed < 0.5  # submit 立即返回，不阻塞调用方
        assert e.status("w1")["status"] in ("pending", "running")
        assert e.wait("w1", timeout=5) == "success"
        assert e.status("w1")["status"] == "success"
    finally:
        e.stop_worker()
