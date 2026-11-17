"""进程重启恢复：把持久化在 SQLite 的运行态重新装配成可继续工作的视图。

桌面进程随时可能被关掉：图执行位置、外部阻塞（等人确认）、未完成处置项、
待重写队列本来就在库里，恢复是「读取式」的——不改写任何状态，因此
阻塞节点 id、处置项状态、幂等键与重启前完全一致。

恢复顺序：图执行位置 -> 待确认清单 -> 待重写队列。
"""
from __future__ import annotations

import json

from itc.store.db import now_iso

_TERMINAL_DISPOSITION_STATES = frozenset({"written_back", "blocked", "failed"})
_TERMINAL_RUN_STATUSES = frozenset({"success", "failed", "cancelled"})
RESTORE_ORDER = ("graph_position", "awaiting_confirm", "pending_writebacks")


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _parse_context(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {"raw": str(value)}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def snapshot(conn) -> dict:
    """恢复前 / 后的一致性快照：阻塞节点、处置项状态、幂等键、待重写键。"""
    dispositions = _rows(conn, "SELECT id,state,idempotency_key FROM dispositions ORDER BY id ASC")
    blocks = _rows(conn, "SELECT node_id FROM external_blocks WHERE status='pending' ORDER BY block_id ASC")
    pendings = _rows(conn, "SELECT idempotency_key FROM pending_writebacks"
                           " WHERE resolved_at IS NULL ORDER BY id ASC")
    return {
        "blocked_nodes": [r["node_id"] for r in blocks],
        "disposition_states": {r["id"]: r["state"] for r in dispositions},
        "idempotency_keys": {r["id"]: r["idempotency_key"] for r in dispositions},
        "pending_writeback_keys": [r["idempotency_key"] for r in pendings],
    }


def recover(conn, run_id: str | None = None) -> dict:
    """扫描并恢复未完成运行态，返回恢复摘要（含 restored / skipped / reason）。

    读取式恢复：不改库、不发写请求；恢复后阻塞节点 id、处置项状态、幂等键
    与重启前一致（由 consistent 字段自检）。
    """
    before = snapshot(conn)
    params: tuple = (run_id,) if run_id else ()

    # 1) 图执行位置：未完成的 graph_runs + 每个 run 最后执行的节点 + 待解除的阻塞。
    run_sql = "SELECT * FROM graph_runs"
    if run_id:
        run_sql += " WHERE run_id=?"
    run_sql += " ORDER BY started_at ASC, run_id ASC"
    all_runs = _rows(conn, run_sql, params)
    graph_position = []
    skipped: list[dict] = []
    for run in all_runs:
        rid = run["run_id"]
        if run["status"] in _TERMINAL_RUN_STATUSES:
            skipped.append({"kind": "graph_run", "id": rid,
                            "reason": f"终态 {run['status']}，无需恢复"})
            continue
        last = conn.execute(
            "SELECT node_id,event_type FROM execution_events WHERE run_id=?"
            " ORDER BY seq DESC LIMIT 1", (rid,)
        ).fetchone()
        blocks = _rows(
            conn,
            "SELECT block_id,node_id,context,status FROM external_blocks"
            " WHERE run_id=? AND status='pending' ORDER BY created_at ASC, block_id ASC",
            (rid,),
        )
        graph_position.append({
            "run_id": rid,
            "transfer_no": run["transfer_no"],
            "status": run["status"],
            "last_node_id": last["node_id"] if last else None,
            "blocked_nodes": [b["node_id"] for b in blocks],
            "reason": "未完成运行：从最后执行节点与外部阻塞处续跑",
        })

    # 2) 待确认清单：外部阻塞（停在等人确认的节点）+ 未完成处置项。
    block_sql = "SELECT * FROM external_blocks WHERE status='pending'"
    if run_id:
        block_sql += " AND run_id=?"
    block_sql += " ORDER BY created_at ASC, block_id ASC"
    external_blocks = []
    for b in _rows(conn, block_sql, params):
        external_blocks.append({
            "block_id": b["block_id"],
            "run_id": b["run_id"],
            "node_id": b["node_id"],
            "context": _parse_context(b.get("context")),
            "status": b["status"],
        })
    released_sql = "SELECT block_id FROM external_blocks WHERE status!='pending'"
    if run_id:
        released_sql += " AND run_id=?"
    for b in _rows(conn, released_sql, params):
        skipped.append({"kind": "external_block", "id": b["block_id"],
                        "reason": "已释放的阻塞，无需恢复"})

    disp_sql = "SELECT * FROM dispositions"
    if run_id:
        disp_sql += " WHERE run_id=?"
    disp_sql += " ORDER BY id ASC"
    dispositions = []
    for d in _rows(conn, disp_sql, params):
        if d["state"] in _TERMINAL_DISPOSITION_STATES:
            skipped.append({"kind": "disposition", "id": d["id"],
                            "reason": f"终态 {d['state']}，无需恢复"})
        else:
            dispositions.append(d)
    awaiting_confirm = [d for d in dispositions if d["state"] == "awaiting_confirm"]

    # 3) 待重写队列：未解决的 pending_writebacks，保留同一幂等键供重试。
    pending_writebacks = _rows(
        conn, "SELECT * FROM pending_writebacks WHERE resolved_at IS NULL ORDER BY id ASC"
    )
    for p in _rows(conn, "SELECT * FROM pending_writebacks"
                         " WHERE resolved_at IS NOT NULL ORDER BY id ASC"):
        skipped.append({"kind": "pending_writeback", "id": p["id"],
                        "reason": "已解决，无需恢复"})

    after = snapshot(conn)
    consistent = before == after
    restored_items = (
        [{"kind": "graph_position", "id": g["run_id"], "reason": g["reason"]}
         for g in graph_position]
        + [{"kind": "external_block", "id": b["block_id"], "reason": "停在节点等人确认"}
           for b in external_blocks]
        + [{"kind": "disposition", "id": d["id"], "reason": f"恢复到原状态 {d['state']}"}
           for d in dispositions]
        + [{"kind": "pending_writeback", "id": p["id"], "reason": "保留同一幂等键待重试"}
           for p in pending_writebacks]
    )
    counts = {
        "graph_position": len(graph_position),
        "awaiting_confirm": len(awaiting_confirm),
        "pending_writebacks": len(pending_writebacks),
        "external_blocks": len(external_blocks),
        "dispositions": len(dispositions),
        "skipped": len(skipped),
    }
    reason = (
        "读取式恢复，未改动持久化状态；"
        "阻塞节点 id / 处置项状态 / 幂等键与重启前一致"
        if consistent else "恢复前后快照不一致，需人工核对"
    )
    blocked_nodes = before["blocked_nodes"]
    summary_line = (
        f"恢复摘要：图执行位置 {counts['graph_position']} 项，"
        f"待确认 {counts['awaiting_confirm']} 项，"
        f"待重写 {counts['pending_writebacks']} 项，"
        f"阻塞节点 {len(blocked_nodes)} 个；跳过 {counts['skipped']} 项"
        f"（原因：终态/已释放/已解决）；{reason}"
    )
    return {
        "restored": {
            "graph_position": graph_position,
            "graph_runs": graph_position,
            "blocked_nodes": blocked_nodes,
            "external_blocks": external_blocks,
            "dispositions": dispositions,
            "awaiting_confirm": awaiting_confirm,
            "pending_writebacks": pending_writebacks,
        },
        "restored_items": restored_items,
        "graph_position": graph_position,
        "blocked_nodes": blocked_nodes,
        "external_blocks": external_blocks,
        "dispositions": dispositions,
        "awaiting_confirm": awaiting_confirm,
        "pending_writebacks": pending_writebacks,
        "skipped": skipped,
        "reason": reason,
        "order": list(RESTORE_ORDER),
        "restore_steps": [
            {"step": "graph_position", "count": counts["graph_position"]},
            {"step": "awaiting_confirm", "count": counts["awaiting_confirm"]},
            {"step": "pending_writebacks", "count": counts["pending_writebacks"]},
        ],
        "counts": counts,
        "consistent": consistent,
        "recovered_at": now_iso(),
        "summary_line": summary_line,
    }
