"""执行侧审计埋点：五类节点事件、事件字段、哈希链断点定位、人工确认操作人。"""
from __future__ import annotations

import json

from itc.graph.audit import (
    emit_node_event, verify_chain, chain_length,
    NODE_EVENT_TYPES,
)
from itc.graph.engine import Engine


_LIFECYCLE = (
    "node_start", "node_end", "node_blocked", "node_retry", "writeback",
)


def _emit_all(db_conn, run_id="run-h"):
    for i, et in enumerate(_LIFECYCLE):
        emit_node_event(
            db_conn, run_id, "n%d" % i,
            event_type=et, node_type="rule_judge",
            payload={"duration_ms": 1.5 * i, "summary": "summary-%d" % i},
        )


def test_five_lifecycle_events_each_produce_one_row_with_fields(db_conn):
    _emit_all(db_conn)
    rows = db_conn.execute("SELECT * FROM execution_events ORDER BY seq").fetchall()
    assert len(rows) == 5
    assert {r["event_type"] for r in rows} == set(_LIFECYCLE)
    assert set(_LIFECYCLE) <= set(NODE_EVENT_TYPES)
    for r in rows:
        assert r["run_id"] == "run-h"           # run_id
        assert r["node_id"].startswith("n")     # 节点 id
        assert r["node_type"] == "rule_judge"   # 节点类型
        assert r["duration_ms"] is not None     # 耗时
        assert r["result_summary"]              # 结果摘要
        assert "summary-" in r["result_summary"]
        assert r["prev_hash"] and r["hash"]


def test_chain_prev_hash_links_and_verify_ok(db_conn):
    _emit_all(db_conn)
    assert chain_length(db_conn) == 5
    ok, bp = verify_chain(db_conn)
    assert ok is True and bp is None
    rows = db_conn.execute(
        "SELECT seq,prev_hash,hash FROM execution_events ORDER BY seq"
    ).fetchall()
    for i in range(1, len(rows)):
        # 每条事件含前一条哈希
        assert rows[i]["prev_hash"] == rows[i - 1]["hash"]


def test_tamper_payload_locates_breakpoint(db_conn):
    _emit_all(db_conn)
    assert verify_chain(db_conn) == (True, None)
    target_seq = 3
    db_conn.execute(
        "UPDATE execution_events SET result_summary=? WHERE seq=?",
        ('{"tampered":1}', target_seq),
    )
    db_conn.commit()
    ok, bp = verify_chain(db_conn)
    assert ok is False
    assert bp == target_seq  # 定位断点位置


def test_tamper_hash_column_locates_breakpoint(db_conn):
    _emit_all(db_conn)
    target_seq = 2
    db_conn.execute(
        "UPDATE execution_events SET hash=? WHERE seq=?", ("deadbeef", target_seq)
    )
    db_conn.commit()
    ok, bp = verify_chain(db_conn)
    assert ok is False and bp == target_seq


def test_manual_confirm_records_operator(db_conn):
    # audit_events：actor 列与载荷都记录操作人
    emit_node_event(
        db_conn, "run-h", "n1", event_type="manual_confirm",
        node_type="rule_judge", actor="护士长-李四",
        payload={"summary": "确认改执行地点"}, table="audit_events",
    )
    row = db_conn.execute(
        "SELECT * FROM audit_events WHERE event_type='manual_confirm'"
    ).fetchone()
    assert row["actor"] == "护士长-李四"
    assert json.loads(row["payload"])["actor"] == "护士长-李四"

    # execution_events：操作人落在载荷（结果摘要）里，且被哈希链覆盖
    emit_node_event(
        db_conn, "run-h", "n2", event_type="manual_confirm",
        actor="护士长-王五", payload={"summary": "确认停嘱草稿"},
    )
    body = db_conn.execute(
        "SELECT result_summary FROM execution_events WHERE node_id='n2'"
    ).fetchone()[0]
    assert "护士长-王五" in body
    assert verify_chain(db_conn) == (True, None)
    assert verify_chain(db_conn, "audit_events") == (True, None)


def test_engine_run_emits_lifecycle_events(db_conn):
    def ex(node_id, info, run_id, transfer_no):
        return {"status": "ok", "summary": "ok", "source": info.get("source", ""),
                "retries": 1}

    g = {
        "A": {"class_type": "fetch_orders", "source": "fetch", "inputs": {}},
        "B": {"class_type": "change_site", "source": "writeback",
              "inputs": {"order_no": "O1", "execute_dept_code": "D", "effective_from": "X"}},
    }
    e_ok = Engine(db_conn, executor=ex)
    e_ok.submit("ok-h", g, transfer_no="T-1")
    e_ok.run("ok-h")

    # 再跑一个被阻断的 run，产生 node_blocked
    def blk(node_id, info, run_id, transfer_no):
        return {"status": "blocked", "summary": "需人工"}
    e_blk = Engine(db_conn, executor=blk)
    e_blk.submit("blk-h", {"A": {"inputs": {}}})
    e_blk.run("blk-h")

    types = {
        r["event_type"] for r in db_conn.execute("SELECT event_type FROM execution_events")
    }
    for et in ("node_start", "node_end", "node_retry", "writeback", "node_blocked"):
        assert et in types
