"""任务 37：节点级重试、退避、熔断与审计。"""
from __future__ import annotations

import json

import pytest

from itc.graph.retry import (
    CircuitBreaker,
    NodeRetryError,
    RetryPolicy,
    default_is_retryable,
    run_with_retry,
)


class Fake5xxError(Exception):
    def __init__(self, code=503):
        super().__init__(f"HIS 5xx: {code}")
        self.status_code = code


class Fake4xxError(Exception):
    def __init__(self, code=400):
        super().__init__(f"HIS 4xx: {code}")
        self.status_code = code


class FakeNetworkError(Exception):
    """模拟网络类可重试错误（按异常类名归类）。"""

    default_message = "HIS 网络连接失败"


class FakeTimeoutError(TimeoutError):
    """模拟节点超时类可重试错误。"""

    default_message = "HIS 请求超时"


def test_5xx_retried_at_most_twice_with_backoff_and_audit(db_conn):
    policy = RetryPolicy(max_retries=2, backoff_base=0.01, breaker_threshold=99)
    calls = []
    delays = []

    def fn():
        calls.append(1)
        raise Fake5xxError(503)

    def on_retry(attempt, error, delay):
        delays.append(delay)

    with pytest.raises(NodeRetryError) as exc_info:
        run_with_retry(
            fn, policy, on_retry=on_retry, conn=db_conn, run_id="run-1",
            node_id="fetch_orders", node_type="fetch_orders",
            downstream=["judge_orders", "writeback"],
        )

    assert len(calls) == 3  # 首次 + 最多 2 次重试
    assert exc_info.value.attempts == 2
    assert delays == pytest.approx([0.01, 0.02])  # 间隔指数退避

    retries = db_conn.execute(
        "SELECT payload FROM audit_events WHERE event_type = 'node_retry' ORDER BY seq ASC"
    ).fetchall()
    assert len(retries) == 2  # 每次重试一条审计事件
    for index, row in enumerate(retries, start=1):
        payload = json.loads(row["payload"])
        assert payload["attempt"] == index
        assert payload["reason"]  # 带原因
        assert "duration_ms" in payload  # 带耗时
        assert payload["backoff_delay"] > 0


def test_non_retryable_error_not_retried(db_conn):
    policy = RetryPolicy(max_retries=2, backoff_base=0.0)
    calls = []

    def fn():
        calls.append(1)
        raise Fake4xxError(400)

    with pytest.raises(Fake4xxError):
        run_with_retry(fn, policy, conn=db_conn, run_id="r", node_id="n")
    assert len(calls) == 1
    retries = db_conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type = 'node_retry'"
    ).fetchone()[0]
    assert retries == 0


def test_success_returns_result_and_resets_breaker(db_conn):
    breaker = CircuitBreaker(threshold=3)
    policy = RetryPolicy(max_retries=2)
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert run_with_retry(fn, policy, breaker=breaker, conn=db_conn, run_id="r", node_id="n") == "ok"
    assert len(calls) == 1
    assert breaker.is_closed
    assert breaker.failures == 0


def test_circuit_breaker_trips_blocks_downstream_and_stops_his_calls(db_conn):
    policy = RetryPolicy(max_retries=2, backoff_base=0.0, breaker_threshold=3)
    breaker = CircuitBreaker(threshold=3)
    calls = []

    def fn():
        calls.append(1)  # 每次调用代表一次下游 HIS 请求
        raise Fake5xxError(503)

    with pytest.raises(NodeRetryError) as first:
        run_with_retry(
            fn, policy, breaker=breaker, conn=db_conn, run_id="run-2",
            node_id="fetch_orders", downstream=["judge_orders", "writeback"],
        )
    assert len(calls) == 3  # 连续 3 次失败
    assert breaker.is_open is True
    assert breaker.failures == 3
    assert first.value.breaker_open is True
    assert first.value.blocks("judge_orders") is True

    # 熔断后再次调度同一节点：不再调用下游 HIS
    with pytest.raises(NodeRetryError) as second:
        run_with_retry(
            fn, policy, breaker=breaker, conn=db_conn, run_id="run-2",
            node_id="fetch_orders", downstream=["judge_orders", "writeback"],
        )
    assert len(calls) == 3  # 下游 HIS 调用次数不增加
    assert second.value.breaker_open is True
    assert "熔断" in second.value.reason  # 可读原因
    assert second.value.blocks("judge_orders") is True

    opened = db_conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type = 'node_circuit_open'"
    ).fetchone()[0]
    assert opened == 1
    blocked = db_conn.execute(
        "SELECT node_id, payload FROM audit_events WHERE event_type = 'node_blocked'"
    ).fetchall()
    assert {r["node_id"] for r in blocked} >= {"judge_orders", "writeback"}
    assert all(json.loads(r["payload"])["blocked_by"] == "fetch_orders" for r in blocked)


def test_timeout_is_retried():
    from itc.graph.cancel import NodeTimeoutError

    policy = RetryPolicy(max_retries=2, backoff_base=0.0)
    calls = []

    def fn():
        calls.append(1)
        raise NodeTimeoutError("节点超时")

    with pytest.raises(NodeRetryError):
        run_with_retry(fn, policy, node_id="n")
    assert len(calls) == 3
    assert default_is_retryable(NodeTimeoutError("x")) is True


def test_default_is_retryable_matrix():
    assert default_is_retryable(Fake5xxError(500)) is True
    assert default_is_retryable(Fake5xxError(599)) is True
    assert default_is_retryable(Fake4xxError(404)) is False
    assert default_is_retryable(FakeNetworkError()) is True
    assert default_is_retryable(FakeTimeoutError()) is True
    assert default_is_retryable(ValueError("字段缺失")) is False

    class NonRetryableError(Exception):
        """名称含 non-retryable，应被判为不可重试。"""

        retryable = False

    assert default_is_retryable(NonRetryableError()) is False


def test_custom_is_retryable_override():
    policy = RetryPolicy(max_retries=1, backoff_base=0.0)
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("按自定义规则可重试")

    with pytest.raises(NodeRetryError):
        run_with_retry(fn, policy, is_retryable=lambda exc: True, node_id="n")
    assert len(calls) == 2
