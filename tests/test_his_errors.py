"""H 组：出站错误分类、指数退避重试与待重写队列（任务 20）。"""
import httpx
import pytest

from itc.his import errors
from itc.his.client import HisClient
from itc.his.writer import HisWriter

BASE = "https://his.test"
TOKEN = "test-token"


def _client(handler):
    return HisClient(
        base_url=BASE, token=TOKEN, transport=httpx.MockTransport(handler)
    )


def _exec_kwargs():
    return dict(
        order_id="O-1",
        inpatient_no="20260001",
        order_no="O-1",
        execute_dept_code="A",
        execute_dept_name="目标科室",
        effective_from="2026-01-01T09:00:00+08:00",
        transfer_no="T-1",
    )


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422])
def test_4xx_non_retryable(code):
    assert errors.classify_status(code) == "non_retryable"
    assert errors.is_retryable(errors.NonRetryableHisError("x", status_code=code)) is False


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_5xx_retryable(code):
    assert errors.classify_status(code) == "retryable"
    assert errors.is_retryable(errors.RetryableHisError("x", status_code=code)) is True


def test_network_error_retryable():
    assert errors.is_retryable(errors.NetworkHisError("x")) is True
    assert errors.is_retryable(httpx.ConnectError("x")) is True


def test_field_missing_not_retryable():
    from itc.domain.models import FieldMissingError

    assert errors.is_retryable(FieldMissingError("x")) is False


def test_retry_with_backoff_retries_twice_then_raises():
    calls = []

    def fn():
        calls.append(1)
        raise errors.RetryableHisError("503", status_code=503)

    with pytest.raises(errors.RetryableHisError):
        errors.retry_with_backoff(fn, max_retries=2, base_delay=0.0)
    assert len(calls) == 3  # 1 次原始 + 2 次重试


def test_retry_with_backoff_non_retryable_no_retry():
    calls = []

    def fn():
        calls.append(1)
        raise errors.NonRetryableHisError("400", status_code=400)

    with pytest.raises(errors.NonRetryableHisError):
        errors.retry_with_backoff(fn, max_retries=2, base_delay=0.0)
    assert len(calls) == 1


def test_retry_with_backoff_on_retry_callback():
    seen = []

    def fn():
        raise errors.RetryableHisError("503")

    with pytest.raises(errors.RetryableHisError):
        errors.retry_with_backoff(
            fn,
            max_retries=2,
            base_delay=0.0,
            on_retry=lambda a, e: seen.append(a),
        )
    assert seen == [1, 2]


def test_retry_with_backoff_success_after_retry():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise errors.RetryableHisError("503")
        return "ok"

    assert errors.retry_with_backoff(fn, max_retries=2, base_delay=0.0) == "ok"
    assert len(calls) == 3


def test_5xx_exhausts_retries_and_queues_pending(db_conn):
    # 写回 5xx：指数退避 <= 2 次，超次置 pending_writeback 并给出提示。
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, json={"e": "down"})

    c = _client(handler)
    w = HisWriter(c, conn=db_conn, max_retries=2, base_delay=0.0)
    res = w.write_execution_site(**_exec_kwargs())
    assert len(calls) == 3
    assert res["pending_writeback"] is True
    assert res["attempts"] == 3
    assert "待重写队列" in res["hint"]
    rows = db_conn.execute("SELECT * FROM pending_writebacks").fetchall()
    assert len(rows) == 1
    assert rows[0]["attempts"] == 3
    assert rows[0]["idempotency_key"]
    # 每次重试写审计事件（只增不改）。
    audits = db_conn.execute(
        "SELECT * FROM audit_events WHERE event_type='writeback_retry'"
    ).fetchall()
    assert len(audits) == 2
    c.close()


def test_4xx_direct_failure_no_pending(db_conn):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"e": "bad"})

    c = _client(handler)
    w = HisWriter(c, conn=db_conn)
    with pytest.raises(errors.NonRetryableHisError):
        w.write_execution_site(**_exec_kwargs())
    assert len(calls) == 1
    assert db_conn.execute(
        "SELECT COUNT(*) FROM pending_writebacks"
    ).fetchone()[0] == 0
    c.close()


def test_network_error_exhausts_and_queues(db_conn):
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ConnectError("boom")

    c = _client(handler)
    w = HisWriter(c, conn=db_conn, max_retries=2, base_delay=0.0)
    res = w.write_execution_site(**_exec_kwargs())
    assert len(calls) == 3
    assert res["pending_writeback"] is True
    assert db_conn.execute(
        "SELECT COUNT(*) FROM pending_writebacks"
    ).fetchone()[0] == 1
    c.close()


def test_queue_pending_writeback_direct(db_conn):
    wid = errors.queue_pending_writeback(
        db_conn,
        disposition_id=7,
        idempotency_key="k1",
        attempts=3,
        reason="r",
    )
    row = db_conn.execute(
        "SELECT * FROM pending_writebacks WHERE id=?", (wid,)
    ).fetchone()
    assert row["disposition_id"] == 7
    assert row["attempts"] == 3 and row["reason"] == "r"
    assert row["resolved_at"] is None
