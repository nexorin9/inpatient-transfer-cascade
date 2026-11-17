"""台账：按转科单号 / 状态 / 科室 / 时间范围只读筛选，导出脱敏，审计只增不改。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from itc.domain.models import DispositionItem
from itc.store import ledger as lg
from itc.store.db import insert_disposition

RAW_INPATIENT_NO = "55667788"


def _seed(conn):
    rows = [
        ("T-100", "written_back", "DEPT-A", "2020-01-01T00:00:00+00:00", "ORD-1"),
        ("T-100", "awaiting_confirm", "DEPT-A", "2026-06-01T00:00:00+00:00", "ORD-2"),
        ("T-200", "blocked", "DEPT-B", "2026-06-02T00:00:00+00:00", "ORD-3"),
    ]
    for transfer_no, state, dept, created_at, ref in rows:
        item_id = insert_disposition(conn, DispositionItem(
            transfer_no=transfer_no, inpatient_no=RAW_INPATIENT_NO, kind="order",
            ref=ref, state=state, execute_dept_code=dept,
            execute_dept_name=f"科室-{dept}", run_id="RUN-1",
        ))
        conn.execute("UPDATE dispositions SET created_at=? WHERE id=?", (created_at, item_id))
    conn.commit()


def test_query_by_transfer_no(db_conn):
    _seed(db_conn)
    rows = lg.query_ledger(db_conn, transfer_no="T-100")
    assert len(rows) == 2
    assert all(r["transfer_no"] == "T-100" for r in rows)


def test_query_by_state_and_department(db_conn):
    _seed(db_conn)
    assert [r["transfer_no"] for r in lg.query_ledger(db_conn, state="blocked")] == ["T-200"]
    assert len(lg.query_ledger(db_conn, execute_dept_code="DEPT-A")) == 2
    assert len(lg.query_ledger(db_conn, transfer_no="T-100", state="awaiting_confirm")) == 1
    assert all(r["execute_dept_code"] == "DEPT-B"
               for r in lg.query_ledger(db_conn, execute_dept_code="DEPT-B"))


def test_query_by_time_range(db_conn):
    _seed(db_conn)
    rows = lg.query_ledger(db_conn, from_ts="2026-01-01", to_ts="2026-12-31")
    assert len(rows) == 2
    assert all(r["created_at"] >= "2026-01-01" for r in rows)
    assert len(lg.query_ledger(db_conn, to_ts="2021-01-01")) == 1
    assert len(lg.query_ledger(db_conn, from_ts="2030-01-01")) == 0


def test_query_supports_inpatient_and_kind_and_limit(db_conn):
    _seed(db_conn)
    assert len(lg.query_ledger(db_conn, inpatient_no=RAW_INPATIENT_NO)) == 3
    assert len(lg.query_ledger(db_conn, kind="order", limit=2)) == 2
    assert lg.query_ledger(db_conn, kind="specimen") == []
    with pytest.raises(ValueError):
        lg.query_ledger(db_conn, unknown_filter="x")


def test_query_is_read_only(db_conn, tmp_path):
    _seed(db_conn)
    changes = db_conn.total_changes
    lg.query_ledger(db_conn, transfer_no="T-100")
    lg.export_ledger(db_conn, tmp_path / "ledger.json")
    assert db_conn.total_changes == changes


def test_export_ledger_is_redacted(db_conn, tmp_path):
    _seed(db_conn)
    out = lg.export_ledger(db_conn, tmp_path / "ledger.json", transfer_no="T-100")
    assert isinstance(out, Path) and out.exists()
    text = out.read_text(encoding="utf-8")
    assert RAW_INPATIENT_NO not in text  # 住院号原文不落导出
    data = json.loads(text)
    assert len(data) == 2
    for row in data:
        assert row["inpatient_no"] == lg.digest_inpatient_no(RAW_INPATIENT_NO)
        assert len(row["inpatient_no"]) == 12
        assert row["transfer_no"] == "T-100"
        assert "execute_dept_name" in row  # 科室名不是 PHI，不应被误删


def test_export_audit_is_redacted(db_conn, tmp_path):
    lg.append_audit_event(db_conn, run_id="R-1", node_id="N-1", event_type="write_back",
                          actor="护士长",
                          payload={"inpatient_no": RAW_INPATIENT_NO, "name": "张三",
                                   "phone": "13800000000", "order_no": "ORD-1"})
    out = lg.export_audit(db_conn, tmp_path / "audit.json")
    text = out.read_text(encoding="utf-8")
    assert RAW_INPATIENT_NO not in text
    assert "张三" not in text
    assert "13800000000" not in text
    data = json.loads(text)
    assert len(data) == 1
    assert "hash" in data[0] and "prev_hash" in data[0]


def test_redact_record_drops_phi_but_keeps_business_fields():
    rec = {
        "inpatient_no": RAW_INPATIENT_NO, "name": "张三", "id_no": "X1",
        "phone": "138", "contact": "Y", "address": "Z",
        "transfer_no": "T-1", "execute_dept_name": "呼吸内科",
        "payload": json.dumps({"inpatient_no": RAW_INPATIENT_NO, "phone": "138"}),
    }
    out = lg.redact_record(rec)
    for key in ("name", "id_no", "phone", "contact", "address"):
        assert key not in out
    assert out["inpatient_no"] == lg.digest_inpatient_no(RAW_INPATIENT_NO)
    assert out["execute_dept_name"] == "呼吸内科"
    assert out["transfer_no"] == "T-1"
    nested = json.loads(out["payload"])
    assert "phone" not in nested
    assert nested["inpatient_no"] == out["inpatient_no"]


def test_audit_append_only_grows_chain(db_conn):
    assert lg.chain_length(db_conn) == 0
    digest = lg.append_audit_event(db_conn, run_id="R-1", node_id="N-1",
                                   event_type="confirm", actor="护士长",
                                   payload={"inpatient_no": RAW_INPATIENT_NO})
    assert len(digest) == 64
    assert lg.chain_length(db_conn) == 1
    lg.append_audit_event(db_conn, run_id="R-1", node_id="N-2", event_type="write_back")
    assert lg.chain_length(db_conn) == 2


def test_ledger_source_has_no_audit_modification_entry():
    """代码层不提供审计表修改入口，也不含改写 / 删除审计表的 SQL。"""
    source = Path(lg.__file__).read_text(encoding="utf-8")
    for table in ("audit_events", "execution_events"):
        assert not re.search(rf"(?i)\bupdate\s+{table}\b", source)
        assert not re.search(rf"(?i)\bdelete\s+from\s+{table}\b", source)
    for name in ("update_audit_event", "delete_audit_event", "update_audit", "delete_audit",
                 "modify_audit", "purge_audit"):
        assert not hasattr(lg, name)
    assert hasattr(lg, "append_audit_event")


def test_ledger_rejects_unknown_audit_table(db_conn):
    with pytest.raises(ValueError):
        lg.append_audit_event(db_conn, run_id="R", node_id="N", event_type="x",
                              table="dispositions")
    with pytest.raises(ValueError):
        lg.verify_audit_chain(db_conn, table="dispositions")
