"""任务 38：节点级超时、全局取消与优雅中断。"""
from __future__ import annotations

import time

import pytest

from itc.graph.cancel import (
    CancelledError,
    CancellationToken,
    GracefulRunner,
    NodeTimeoutError,
    graceful_interrupt,
    load_resume_state,
    node_timeout,
    recoverable_nodes,
)
from itc.graph.retry import default_is_retryable


def test_token_cancel_sets_flag_and_reason():
    token = CancellationToken()
    assert token.is_cancelled is False
    token.cancel("护士长关窗")
    assert token.is_cancelled is True
    assert token.reason == "护士长关窗"
    # 首次取消原因不被后续 cancel 覆盖
    token.cancel("第二次原因")
    assert token.reason == "护士长关窗"


def test_token_check_raises_cancelled_error():
    token = CancellationToken()
    token.cancel("进程被停")
    with pytest.raises(CancelledError) as exc_info:
        token.check()
    assert "进程被停" in str(exc_info.value)


def test_node_timeout_raises_retryable_error():
    with pytest.raises(NodeTimeoutError):
        with node_timeout(0.01):
            time.sleep(0.05)
    assert default_is_retryable(NodeTimeoutError("t")) is True


def test_node_timeout_does_not_fire_for_fast_block():
    completed = False
    with node_timeout(1.0):
        completed = (1 + 1 == 2)
    assert completed is True


def test_cancellation_stops_new_nodes_and_drains_in_flight():
    token = CancellationToken()
    issued = []

    def dispatch(node_id):
        issued.append(node_id)  # 一次下游 HIS 请求
        if node_id == "node_2":
            token.cancel("护士长关窗")  # 在途节点执行中收到取消

    runner = GracefulRunner(token, dispatch)
    result = runner.run(["node_1", "node_2", "node_3", "node_4"])

    assert result["cancelled"] is True
    assert result["requests"] == ["node_1", "node_2"]  # 停止调度新节点
    assert "node_3" not in result["requests"]  # 取消后无新请求发出
    assert "node_4" not in result["requests"]
    assert issued == ["node_1", "node_2"]
    assert result["completed"] == ["node_1", "node_2"]  # 在途节点允许收尾
    assert result["in_flight"] == []  # 等待在途节点收尾完成


def test_graceful_interrupt_marks_recoverable_with_summary(db_conn):
    token = CancellationToken()
    token.cancel("HIS 卡住，用户取消")
    states = {
        "fetch_orders": "success",
        "judge_orders": "running",
        "writeback": "pending",
        "done_node": "done",
    }
    summary = graceful_interrupt(db_conn, "run-1", token, node_states=states)

    assert summary["cancelled"] is True
    assert "HIS 卡住" in summary["reason"]
    assert summary["unfinished_count"] == 2
    ids = [u["node_id"] for u in summary["unfinished_nodes"]]
    assert set(ids) == {"judge_orders", "writeback"}
    assert all(u["recoverable"] is True for u in summary["unfinished_nodes"])
    assert all(u["state"] == "recoverable" for u in summary["unfinished_nodes"])
    assert all(u["reason"] for u in summary["unfinished_nodes"])

    interrupted = db_conn.execute(
        "SELECT payload FROM audit_events WHERE event_type = 'run_interrupted'"
    ).fetchall()
    assert len(interrupted) == 1
    recoverable = db_conn.execute(
        "SELECT node_id FROM audit_events WHERE event_type = 'node_recoverable'"
    ).fetchall()
    assert {r["node_id"] for r in recoverable} == {"judge_orders", "writeback"}


def test_resume_state_survives_process_restart(db_conn, tmp_path):
    from itc.store.db import connect

    token = CancellationToken()
    token.cancel("进程被停")
    graceful_interrupt(
        db_conn, "run-2", token,
        node_states={"n1": "running", "n2": "pending", "n3": "success"},
    )

    # 新连接读取同一状态库，模拟进程重启后续跑
    conn2 = connect(str(tmp_path / "state.sqlite3"))
    try:
        nodes = recoverable_nodes(conn2, "run-2")
        assert {n["node_id"] for n in nodes} == {"n1", "n2"}
        assert all(n["recoverable"] is True for n in nodes)
        state = load_resume_state(conn2, "run-2")
        assert state is not None
        assert state["recoverable"] is True
        assert state["unfinished_count"] == 2
        assert "进程被停" in state["reason"]
    finally:
        conn2.close()


def test_graceful_interrupt_without_explicit_states_reads_execution_events(db_conn):
    from itc.store.db import append_audit

    token = CancellationToken()
    token.cancel("取消")
    append_audit(db_conn, run_id="run-3", node_id="a", event_type="node_started", table="execution_events")
    append_audit(db_conn, run_id="run-3", node_id="b", event_type="node_started", table="execution_events")
    append_audit(db_conn, run_id="run-3", node_id="a", event_type="node_success", table="execution_events")

    summary = graceful_interrupt(db_conn, "run-3", token)
    # a 已完成（有终态事件），b 仍在 running → 只有 b 未完成
    assert [u["node_id"] for u in summary["unfinished_nodes"]] == ["b"]
