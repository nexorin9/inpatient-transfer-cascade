"""写回幂等登记表：键与业务字段绑定、重复提交只 1 次 POST、并发与跨进程保护。"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from itc.store.db import connect
from itc.store.writeback_registry import (
    WritebackRegistry, is_success, make_idempotency_key,
)


def _db_path(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()["file"]


class FakeClient:
    """测试替身：只计数 POST，不产生真实网络请求。"""

    def __init__(self):
        self.posts: list[dict] = []

    def post(self, path, json=None, headers=None):
        self.posts.append({"path": path, "json": json, "headers": headers})
        return {"status_code": 200, "body": {"ok": True}}


def _do_write(reg: WritebackRegistry, key: str, client: FakeClient,
              disposition_id: int = 1):
    def fn():
        resp = client.post(
            "/api/v1/orders/ORD-1/execution-site",
            json={"execute_dept_code": "DEPT-2"},
            headers={"Idempotency-Key": key},
        )
        return {
            "status_code": resp["status_code"],
            "response_digest": "digest-of-response",
            "transfer_no": "T-100",
            "order_no": "ORD-1",
            "disposition_id": disposition_id,
        }
    return reg.concurrent_write(key, fn)


def test_idempotency_key_depends_only_on_business_fields():
    k1 = make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01T08:00:00")
    k2 = make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01T08:00:00")
    assert k1 == k2
    assert len(k1) == 64
    # 改 effective_from 后键不同
    assert make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-02T08:00:00") != k1
    assert make_idempotency_key("T-100", "ORD-1", "DEPT-9", "2026-01-01T08:00:00") != k1
    assert make_idempotency_key("T-999", "ORD-1", "DEPT-2", "2026-01-01T08:00:00") != k1
    # 按契约调用 G1：可用时结果必须一致
    try:
        from itc.graph.idempotency import idempotency_key
    except ImportError:
        idempotency_key = None
    if idempotency_key is not None:
        assert idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01T08:00:00") == k1


def test_record_roundtrip_contains_result_fields(db_conn):
    reg = WritebackRegistry(db_conn)
    key = make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01")
    row = reg.record(key, transfer_no="T-100", order_no="ORD-1", status_code=201,
                     response_digest="digest-X", disposition_id=7)
    assert row["status_code"] == 201
    assert row["response_digest"] == "digest-X"
    assert row["disposition_id"] == 7
    assert row["created_at"]
    hit = reg.check(key)
    assert hit["idempotency_key"] == key
    assert hit["transfer_no"] == "T-100"
    assert is_success(hit) is True
    # 已成功后重复登记不覆盖原结果
    reg.record(key, transfer_no="OTHER", status_code=500, response_digest="err")
    assert reg.check(key)["status_code"] == 201
    assert reg.check("missing-key") is None


def test_duplicate_submit_only_one_post(db_conn):
    reg = WritebackRegistry(db_conn)
    key = make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01")
    client = FakeClient()
    first = _do_write(reg, key, client)
    assert first["reused"] is False
    assert len(client.posts) == 1
    assert client.posts[0]["headers"]["Idempotency-Key"] == key
    second = _do_write(reg, key, client)
    assert second["reused"] is True
    assert len(client.posts) == 1  # 命中成功记录，跳过请求
    assert second["status_code"] == 200
    assert second["response_digest"] == "digest-of-response"
    assert second["disposition_id"] == 1
    rows = db_conn.execute("SELECT * FROM writeback_registry").fetchall()
    assert len(rows) == 1


def test_cross_process_reopen_does_not_repeat_post(db_conn):
    reg = WritebackRegistry(db_conn)
    key = make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01")
    client1 = FakeClient()
    _do_write(reg, key, client1)
    assert len(client1.posts) == 1
    # 模拟新进程：新建连接指向同一 SQLite 文件，用全新的假 client 再提交一次
    conn2 = connect(_db_path(db_conn))
    try:
        reg2 = WritebackRegistry(conn2)
        client2 = FakeClient()
        reused = _do_write(reg2, key, client2)
        assert len(client2.posts) == 0
        assert reused["reused"] is True
        assert reused["idempotency_key"] == key
        assert reg2.check(key)["status_code"] == 200
    finally:
        conn2.close()
    assert len(client1.posts) == 1


def test_db_unique_constraint_blocks_duplicate_key(db_conn):
    key = make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01")
    db_conn.execute(
        "INSERT INTO writeback_registry(idempotency_key,transfer_no,order_no) VALUES (?,?,?)",
        (key, "T-100", "ORD-1"),
    )
    db_conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO writeback_registry(idempotency_key,transfer_no,order_no) VALUES (?,?,?)",
            (key, "T-100", "ORD-1"),
        )
        db_conn.commit()


def test_in_process_lock_serializes_concurrent_writes(db_conn):
    reg = WritebackRegistry(db_conn)
    key = make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01")
    client = FakeClient()
    results: list[dict] = []
    lock = threading.Lock()

    def worker():
        r = _do_write(reg, key, client)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(client.posts) == 1  # 进程内锁保证只发一次
    assert sum(1 for r in results if not r["reused"]) == 1
    assert all(r["idempotency_key"] == key for r in results)


def test_failed_writeback_allows_retry_with_same_key(db_conn):
    reg = WritebackRegistry(db_conn)
    key = make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01")
    reg.record(key, transfer_no="T-100", order_no="ORD-1", status_code=503,
               response_digest="server-error", disposition_id=1)
    assert reg.check_success(key) is None
    client = FakeClient()
    retried = _do_write(reg, key, client)
    assert len(client.posts) == 1
    assert retried["reused"] is False
    assert reg.check(key)["status_code"] == 200
    assert reg.check(key)["idempotency_key"] == key


def test_pending_queue_keeps_same_key_for_retry(db_conn):
    reg = WritebackRegistry(db_conn)
    key = make_idempotency_key("T-100", "ORD-1", "DEPT-2", "2026-01-01")
    pending_id = reg.queue_pending(key, disposition_id=3, attempts=2, reason="5xx 超次")
    pending = reg.pending()
    assert len(pending) == 1
    assert pending[0]["idempotency_key"] == key
    assert pending[0]["reason"] == "5xx 超次"
    assert pending[0]["resolved_at"] is None
    assert reg.resolve_pending(pending_id) is True
    assert reg.pending() == []
    assert reg.resolve_pending(pending_id) is False  # 幂等：不重复解决
