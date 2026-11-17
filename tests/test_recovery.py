"""进程重启恢复：图执行位置 -> 待确认清单 -> 待重写队列；恢复前后状态一致。"""
from __future__ import annotations

import json

from itc.domain.models import DispositionItem
from itc.store import recovery as rc
from itc.store.db import append_audit, connect, insert_disposition, now_iso


def _db_path(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()["file"]


def _seed(conn):
    ts = now_iso()
    conn.execute(
        "INSERT INTO graph_runs(run_id,transfer_no,status,started_at) VALUES (?,?,?,?)",
        ("RUN-1", "T-RECOVER", "running", ts),
    )
    conn.execute(
        "INSERT INTO graph_runs(run_id,transfer_no,status,started_at,finished_at) VALUES (?,?,?,?,?)",
        ("RUN-0", "T-DONE", "success", ts, ts),
    )
    conn.execute(
        "INSERT INTO external_blocks(block_id,run_id,node_id,context,status,created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("B-1", "RUN-1", "node-wait-confirm",
         json.dumps({"reason": "需医师判断"}, ensure_ascii=False), "pending", ts),
    )
    conn.execute(
        "INSERT INTO external_blocks(block_id,run_id,node_id,status,created_at,released_at)"
        " VALUES (?,?,?,?,?,?)",
        ("B-0", "RUN-1", "node-old", "released", ts, ts),
    )
    insert_disposition(conn, DispositionItem(
        transfer_no="T-RECOVER", inpatient_no="770011", kind="order", ref="ORD-1",
        state="awaiting_confirm", idempotency_key="IDEM-AWAIT", run_id="RUN-1",
    ))
    insert_disposition(conn, DispositionItem(
        transfer_no="T-RECOVER", inpatient_no="770011", kind="specimen", ref="SP-1",
        state="scoped", idempotency_key="IDEM-SCOPED", run_id="RUN-1",
    ))
    insert_disposition(conn, DispositionItem(
        transfer_no="T-RECOVER", inpatient_no="770011", kind="order", ref="ORD-2",
        state="written_back", idempotency_key="IDEM-DONE", run_id="RUN-1",
    ))
    conn.execute(
        "INSERT INTO pending_writebacks(disposition_id,idempotency_key,attempts,reason,created_at)"
        " VALUES (?,?,?,?,?)",
        (1, "IDEM-PENDING", 2, "5xx 超次", ts),
    )
    conn.execute(
        "INSERT INTO pending_writebacks(disposition_id,idempotency_key,attempts,reason,created_at,resolved_at)"
        " VALUES (?,?,?,?,?,?)",
        (2, "IDEM-RESOLVED", 1, "已重试成功", ts, ts),
    )
    # 只执行到取数节点，下游写回节点未执行。
    append_audit(conn, run_id="RUN-1", node_id="node-fetch", event_type="node_done",
                 table="execution_events", payload={"duration_ms": 3.0})
    conn.commit()


def test_recovery_after_restart_keeps_state(db_conn):
    _seed(db_conn)
    before = rc.snapshot(db_conn)
    assert before["blocked_nodes"] == ["node-wait-confirm"]

    # 模拟关进程重开：新连接指向同一 SQLite 文件。
    conn2 = connect(_db_path(db_conn))
    try:
        summary = rc.recover(conn2)
        assert summary["order"] == ["graph_position", "awaiting_confirm", "pending_writebacks"]
        assert [s["step"] for s in summary["restore_steps"]] == list(summary["order"])
        assert summary["consistent"] is True
        assert summary["blocked_nodes"] == ["node-wait-confirm"]
        assert summary["restored"]["blocked_nodes"] == ["node-wait-confirm"]
        assert summary["external_blocks"][0]["node_id"] == "node-wait-confirm"
        assert summary["external_blocks"][0]["context"]["reason"] == "需医师判断"

        # 处置项状态与幂等键与重启前一致。
        assert summary["dispositions"]
        for d in summary["dispositions"]:
            assert d["state"] == before["disposition_states"][d["id"]]
            assert d["idempotency_key"] == before["idempotency_keys"][d["id"]]
        by_state = {d["state"]: d for d in summary["dispositions"]}
        assert by_state["awaiting_confirm"]["idempotency_key"] == "IDEM-AWAIT"
        assert by_state["scoped"]["idempotency_key"] == "IDEM-SCOPED"
        assert summary["awaiting_confirm"][0]["id"] == by_state["awaiting_confirm"]["id"]

        # 待重写队列保留同一幂等键，且停在原节点等确认、下游未执行。
        assert len(summary["pending_writebacks"]) == 1
        assert summary["pending_writebacks"][0]["idempotency_key"] == "IDEM-PENDING"
        assert summary["pending_writebacks"][0]["idempotency_key"] in before["pending_writeback_keys"]
        executed = [r["node_id"] for r in conn2.execute(
            "SELECT node_id FROM execution_events"
        ).fetchall()]
        assert executed == ["node-fetch"]
        assert "node-write-back" not in executed

        # 恢复摘要含恢复了哪些、跳过哪些、原因。
        assert summary["restored_items"]
        assert summary["skipped"]
        assert all(s["reason"] for s in summary["skipped"])
        assert "读取式恢复" in summary["reason"]
        assert "恢复摘要" in summary["summary_line"]
        assert summary["counts"]["graph_position"] == 1
        assert summary["counts"]["awaiting_confirm"] == 1
        assert summary["counts"]["pending_writebacks"] == 1
        assert rc.snapshot(conn2) == before
    finally:
        conn2.close()


def test_skipped_items_have_reasons(db_conn):
    _seed(db_conn)
    summary = rc.recover(db_conn)
    kinds = {s["kind"] for s in summary["skipped"]}
    assert {"graph_run", "external_block", "disposition", "pending_writeback"} <= kinds
    for s in summary["skipped"]:
        assert s["reason"]
    assert not any(d["state"] == "written_back" for d in summary["dispositions"])
    assert not any(b["node_id"] == "node-old" for b in summary["external_blocks"])


def test_recover_is_read_only(db_conn):
    _seed(db_conn)
    changes = db_conn.total_changes
    rc.recover(db_conn)
    assert db_conn.total_changes == changes  # 恢复不改写任何持久化状态
    assert rc.recover(db_conn)["consistent"] is True


def test_recover_with_run_id_filter(db_conn):
    _seed(db_conn)
    summary = rc.recover(db_conn, run_id="RUN-1")
    assert all(g["run_id"] == "RUN-1" for g in summary["graph_position"])
    summary_none = rc.recover(db_conn, run_id="NO-SUCH-RUN")
    assert summary_none["graph_position"] == []
