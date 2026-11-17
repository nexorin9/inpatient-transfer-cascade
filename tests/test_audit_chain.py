"""只增不改审计哈希链：每条含前一条哈希，篡改可定位断点与断链条数。"""
from __future__ import annotations

import json

from itc.store import ledger as lg
from itc.store.db import append_audit

RAW_INPATIENT_NO = "99001122"


def _seed(conn, n: int = 5):
    for i in range(n):
        lg.append_audit_event(
            conn, run_id="R-CHAIN", node_id=f"N-{i}", event_type="write_back",
            node_type="disposition", actor="护士长",
            payload={"inpatient_no": RAW_INPATIENT_NO, "order_no": f"ORD-{i}",
                     "execute_dept_code": "DEPT-2"},
        )


def _rows(conn, table="audit_events"):
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM {table} ORDER BY seq ASC"
    ).fetchall()]


def test_each_record_carries_previous_hash(db_conn):
    _seed(db_conn)
    rows = _rows(db_conn)
    assert len(rows) == 5
    assert rows[0]["prev_hash"] == "0" * 64
    for i in range(1, len(rows)):
        assert rows[i]["prev_hash"] == rows[i - 1]["hash"]
        assert len(rows[i]["hash"]) == 64
    assert lg.chain_length(db_conn) == 5


def test_intact_chain_verifies(db_conn):
    _seed(db_conn)
    assert lg.verify_audit_chain(db_conn) == (True, None)
    detail = lg.verify_audit_chain_detailed(db_conn)
    assert detail["ok"] is True
    assert detail["broken_count"] == 0
    assert detail["break_at"] is None
    assert detail["chain_length"] == 5
    assert lg.broken_chain_count(db_conn) == 0


def test_tampered_payload_is_detected_with_break_position(db_conn):
    _seed(db_conn)
    target = db_conn.execute(
        "SELECT seq,payload FROM audit_events ORDER BY seq LIMIT 1 OFFSET 2"
    ).fetchone()
    tampered = json.loads(target["payload"])
    tampered["order_no"] = "TAMPERED"
    db_conn.execute(
        "UPDATE audit_events SET payload=? WHERE seq=?",
        (json.dumps(tampered, ensure_ascii=False, sort_keys=True), target["seq"]),
    )
    db_conn.commit()
    ok, break_at = lg.verify_audit_chain(db_conn)
    assert ok is False
    assert break_at == target["seq"]
    detail = lg.verify_audit_chain_detailed(db_conn)
    assert detail["break_at"] == target["seq"]
    assert detail["break_index"] == 3
    assert detail["broken_count"] >= 1
    assert lg.broken_chain_count(db_conn) >= 1


def test_tampered_hash_breaks_multiple_links(db_conn):
    _seed(db_conn)
    db_conn.execute("UPDATE audit_events SET hash=? WHERE seq=?", ("deadbeef", 2))
    db_conn.commit()
    ok, break_at = lg.verify_audit_chain(db_conn)
    assert ok is False
    assert break_at == 2
    detail = lg.verify_audit_chain_detailed(db_conn)
    assert detail["broken_count"] >= 2  # 自身哈希不符 + 下一条前驱哈希不符
    assert 2 in [b["seq"] for b in detail["breaks"]]


def test_tamper_later_link_locates_break(db_conn):
    _seed(db_conn, n=4)
    db_conn.execute("UPDATE audit_events SET hash=? WHERE seq=?", ("0" * 64, 3))
    db_conn.commit()
    detail = lg.verify_audit_chain_detailed(db_conn)
    assert detail["ok"] is False
    assert detail["break_at"] == 3
    assert detail["broken_count"] >= 1
    assert detail["chain_length"] == 4


def test_execution_events_chain_also_verifiable(db_conn):
    for i in range(3):
        append_audit(db_conn, run_id="R-CHAIN", node_id=f"E-{i}", event_type="node_done",
                     table="execution_events", payload={"duration_ms": 1.5 * i})
    assert lg.chain_length(db_conn, "execution_events") == 3
    assert lg.verify_audit_chain(db_conn, "execution_events") == (True, None)
    rows = _rows(db_conn, "execution_events")
    assert rows[0]["prev_hash"] == "0" * 64
    assert rows[2]["prev_hash"] == rows[1]["hash"]


def test_audit_event_payload_is_redacted_at_write_time(db_conn):
    digest = lg.append_audit_event(
        db_conn, run_id="R", node_id="N", event_type="confirm",
        payload={"inpatient_no": RAW_INPATIENT_NO, "name": "李四", "id_no": "ID-1"},
    )
    assert len(digest) == 64
    payload = json.loads(_rows(db_conn)[0]["payload"])
    assert RAW_INPATIENT_NO not in json.dumps(payload, ensure_ascii=False)
    assert payload["inpatient_no"] == lg.digest_inpatient_no(RAW_INPATIENT_NO)
    assert "name" not in payload and "id_no" not in payload
    assert lg.verify_audit_chain(db_conn) == (True, None)  # 脱敏后链条仍自洽
