"""任务 33：图级去重、导入批次重复防护、幂等键可见性。"""
from __future__ import annotations

import json

from itc.graph.dedupe import dedupe_import_batch, graph_dedupe, record_drop


class _FakeReader:
    """测试替身：按注入数据返回三类资源，方法名与 H 组 reader 契约一致。"""

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


def _order_rows(n):
    return [
        {
            "order_no": f"O-{i}", "content": "c", "drug_name": "d", "route": "r",
            "frequency": "f", "execute_dept_code": "DEPT-A", "start_at": "t",
        }
        for i in range(n)
    ]


def test_duplicate_graph_submission_hits_existing_run_no_refetch(db_conn):
    from itc.graph.fetch_nodes import counting_reader, execute_fetch_node, make_fetch_node
    from itc.graph.idempotency import idempotency_key

    key = idempotency_key("T-DEDUP", "O-1", "DEPT-A", "2026-01-01T09:00:00+08:00")
    reader = counting_reader(_FakeReader(orders=_order_rows(3)))
    node = make_fetch_node("orders", "10001")
    fetch_calls = {"n": 0}
    judge_calls = {"n": 0}

    def take_and_judge():
        execute_fetch_node(node, reader)
        fetch_calls["n"] += 1
        judge_calls["n"] += 1

    def submit(transfer_no, run_id, key_value):
        allowed = graph_dedupe(db_conn, transfer_no, run_id, key_value)
        if allowed:
            take_and_judge()
        return allowed

    # 首次入图：取数 + 判定各 1 次
    assert submit("T-DEDUP", "run-1", key) is True
    # 同一 transfer_no 重复入图（不同 run_id，业务字段不变 → 幂等键相同）
    same_fields_key = idempotency_key("T-DEDUP", "O-1", "DEPT-A", "2026-01-01T09:00:00+08:00")
    assert same_fields_key == key
    assert submit("T-DEDUP", "run-2", same_fields_key) is False

    assert fetch_calls["n"] == 1
    assert judge_calls["n"] == 1
    assert reader.call_count() == 1
    assert reader.counts.get("read_executing_orders") == 1

    # graph_idempotency 表只登记一次，且保留首个运行
    rows = db_conn.execute(
        "SELECT * FROM graph_idempotency WHERE transfer_no = ?", ("T-DEDUP",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["idempotency_key"] == key


def test_distinct_transfer_no_creates_new_run(db_conn):
    assert graph_dedupe(db_conn, "T-A", "run-a", "key-a") is True
    assert graph_dedupe(db_conn, "T-B", "run-b", "key-b") is True
    assert graph_dedupe(db_conn, "T-A", "run-c", "key-a") is False
    count = db_conn.execute("SELECT COUNT(*) FROM graph_idempotency").fetchone()[0]
    assert count == 2
    # 重复入图写入命中审计事件，便于审计侧观察去重
    hits = db_conn.execute(
        "SELECT payload FROM audit_events WHERE event_type = 'graph_idempotency_hit'"
    ).fetchall()
    assert len(hits) == 1
    assert "T-A" in hits[0]["payload"]


def test_import_batch_dedupe_records_drop_reasons_and_counts(db_conn):
    rows = [
        {"transfer_no": "T-1", "inpatient_no": "1"},
        {"transfer_no": "T-2", "inpatient_no": "2"},
        {"transfer_no": "T-1", "inpatient_no": "1"},  # 重复
        {"transfer_no": "T-3", "inpatient_no": "3"},
        {"transfer_no": "T-1", "inpatient_no": "1"},  # 重复
        {"transfer_no": "T-2", "inpatient_no": "2"},  # 重复
    ]
    kept, drops = dedupe_import_batch(rows)
    assert [r["transfer_no"] for r in kept] == ["T-1", "T-2", "T-3"]
    assert len(drops) == 3
    assert all("重复" in d["reason"] for d in drops)
    assert {d["row_index"] for d in drops} == {2, 4, 5}
    assert drops[0]["duplicate_of"] == 0

    for drop in drops:
        record_drop(db_conn, drop["transfer_no"], drop["reason"])
    count = db_conn.execute("SELECT COUNT(*) FROM import_drops").fetchone()[0]
    assert count == len(drops) == 3
    reasons = [r[0] for r in db_conn.execute("SELECT reason FROM import_drops").fetchall()]
    assert all("重复" in r for r in reasons)


def test_import_batch_handles_domain_objects(sample_transfer):
    rows = [sample_transfer, sample_transfer, {"transfer_no": "T-OTHER"}]
    kept, drops = dedupe_import_batch(rows)
    assert len(kept) == 2
    assert len(drops) == 1
    assert drops[0]["transfer_no"] == sample_transfer.transfer_no


def test_idempotency_key_visible_in_ledger_and_audit(db_conn):
    from itc.domain.models import DispositionItem
    from itc.graph.idempotency import idempotency_key
    from itc.store.db import fetch_dispositions, insert_disposition

    key = idempotency_key("T-9", "O-9", "DEPT-Z", "2026-03-03T10:00:00+08:00")
    assert graph_dedupe(db_conn, "T-9", "run-9", key) is True

    # 台账登记（graph_idempotency）可查到本次幂等键
    row = db_conn.execute(
        "SELECT idempotency_key FROM graph_idempotency WHERE transfer_no = ?", ("T-9",)
    ).fetchone()
    assert row["idempotency_key"] == key

    # 审计可查：payload 含幂等键取值
    payloads = [
        r["payload"]
        for r in db_conn.execute(
            "SELECT payload FROM audit_events WHERE event_type LIKE 'graph_idempotency%'"
        ).fetchall()
    ]
    assert payloads
    assert all(key in payload for payload in payloads)
    assert any(
        json.loads(payload).get("idempotency_key") == key for payload in payloads
    )

    # 处置台账亦携带并可查询该幂等键（底座 ledger 写入 / 查询）
    item = DispositionItem(
        transfer_no="T-9", inpatient_no="10009", kind="order", ref="O-9",
        idempotency_key=key, run_id="run-9",
    )
    insert_disposition(db_conn, item)
    ledger = fetch_dispositions(db_conn, transfer_no="T-9")
    assert len(ledger) == 1
    assert ledger[0]["idempotency_key"] == key
