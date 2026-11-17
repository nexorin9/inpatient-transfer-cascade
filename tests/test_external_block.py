"""持久化外部阻塞：落 SQLite 字段、解除写审计、进程重启恢复、下游不入队。"""
from __future__ import annotations

import json

from itc.store.db import connect, init_schema
from itc.graph.external_block import ExternalBlockStore
from itc.graph.engine import Engine


def _block_graph():
    # A -> B -> C，B 为等待人工判断的节点
    return {
        "A": {"class_type": "fetch_orders", "inputs": {}},
        "B": {"class_type": "model_judge", "inputs": {"orders": ["A", 0]}},
        "C": {"class_type": "change_site", "inputs": {"decision": ["B", 0]}},
    }


def test_create_persists_all_required_columns(db_conn):
    store = ExternalBlockStore(db_conn)
    block_id = store.create("run-1", "B", {"reason": "需医师判断", "order_no": "O-1"})
    assert block_id.startswith("blk-")
    row = db_conn.execute("SELECT * FROM external_blocks WHERE block_id=?", (block_id,)).fetchone()
    assert row is not None
    assert row["run_id"] == "run-1"
    assert row["node_id"] == "B"
    assert row["status"] == "pending"
    assert row["created_at"]                     # 时间
    assert json.loads(row["context"])["reason"] == "需医师判断"
    assert row["released_at"] is None and row["released_by"] is None


def test_pending_and_restore_list_pending_blocks(db_conn):
    store = ExternalBlockStore(db_conn)
    store.create("run-1", "B", {"reason": "需医师判断"})
    store.create("run-2", "D", {"reason": "标本去向歧义"})
    assert [b["node_id"] for b in store.pending("run-1")] == ["B"]
    assert [b["node_id"] for b in store.restore("run-1")] == ["B"]
    assert len(store.pending()) == 2


def test_release_updates_status_and_writes_audit_event(db_conn):
    store = ExternalBlockStore(db_conn)
    block_id = store.create("run-1", "B", {"reason": "需医师判断"})

    out = store.release(block_id, actor="护士长-张三")
    assert out["status"] == "released"
    assert out["released_by"] == "护士长-张三"
    assert out["released_at"]

    row = db_conn.execute("SELECT * FROM external_blocks WHERE block_id=?", (block_id,)).fetchone()
    assert row["status"] == "released"
    assert row["released_by"] == "护士长-张三"
    assert row["released_at"]

    # 审计事件：谁 / 何时 / 解除哪个阻塞
    ev = db_conn.execute(
        "SELECT * FROM audit_events WHERE event_type='external_block_release'"
    ).fetchone()
    assert ev is not None
    assert ev["actor"] == "护士长-张三"            # 谁
    payload = json.loads(ev["payload"])
    assert payload["block_id"] == block_id        # 哪个阻塞
    assert payload["released_at"] == out["released_at"]  # 何时
    assert store.pending("run-1") == []


def test_restart_recovery_same_node_and_downstream_not_executed(tmp_path):
    db_path = str(tmp_path / "block_state.sqlite3")
    # 进程一：登记阻塞后关闭
    conn1 = connect(db_path)
    init_schema(conn1)
    ExternalBlockStore(conn1).create("run-1", "B", {"reason": "需医师判断"})
    conn1.close()

    # 进程二：新建连接指向同一 db 文件
    conn2 = connect(db_path)
    init_schema(conn2)
    store2 = ExternalBlockStore(conn2)
    assert store2.pending("run-1")[0]["node_id"] == "B"
    assert store2.restore("run-1")[0]["node_id"] == "B"

    calls = []

    def fake_executor(node_id, info, run_id, transfer_no):
        calls.append(node_id)
        return {"status": "ok", "summary": "ok", "source": ""}

    eng = Engine(conn2, executor=fake_executor)
    assert eng.submit("run-1", _block_graph()) is True
    result = eng.run("run-1")
    assert result.status == "blocked"
    # 停在 B 等待确认：B 及其下游 C 均未执行（下游不入队）
    assert calls == ["A"]
    assert "B" not in calls
    assert "C" not in calls
    assert eng.status("run-1")["status"] == "blocked"
    conn2.close()
