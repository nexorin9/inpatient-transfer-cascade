"""图执行侧幂等：同一转科单重复入图去重、导入批次重复防护、幂等键登记。

只依赖底座 store.db（append_audit / now_iso / 表结构）与调用方传入的幂等键取值；
幂等键本身由 G1 的 itc.graph.idempotency.idempotency_key 按契约计算后传入。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from itc.store.db import append_audit, now_iso

# 批次内重复转科单号的统一丢弃原因（可读、可审计）
DUPLICATE_TRANSFER_NO_REASON = "导入批次内转科单号重复，仅保留首条，其余丢弃"


def graph_dedupe(
    conn: sqlite3.Connection,
    transfer_no: str,
    run_id: str,
    idempotency_key_value: str,
) -> bool:
    """图级去重。

    同 transfer_no 已入图（graph_idempotency 有登记）返回 False：命中已有运行，
    调用方据此不再取数、不再判定。首次入图则登记幂等键并写审计事件，返回 True。
    幂等键随登记落入 graph_idempotency 表与 audit_events，台账与审计均可查。
    """
    existing = conn.execute(
        "SELECT run_id, idempotency_key FROM graph_idempotency WHERE transfer_no = ?",
        (transfer_no,),
    ).fetchone()
    if existing is not None:
        append_audit(
            conn,
            run_id=run_id,
            node_id=str(transfer_no),
            event_type="graph_idempotency_hit",
            node_type="graph",
            actor="graph_dedupe",
            payload={
                "transfer_no": str(transfer_no),
                "existing_run_id": existing["run_id"],
                "idempotency_key": str(idempotency_key_value),
                "action": "hit_existing_run",
            },
        )
        return False

    conn.execute(
        "INSERT INTO graph_idempotency(transfer_no, run_id, idempotency_key, created_at)"
        " VALUES (?, ?, ?, ?)",
        (str(transfer_no), str(run_id), str(idempotency_key_value), now_iso()),
    )
    conn.commit()
    append_audit(
        conn,
        run_id=run_id,
        node_id=str(transfer_no),
        event_type="graph_idempotency",
        node_type="graph",
        actor="graph_dedupe",
        payload={
            "transfer_no": str(transfer_no),
            "idempotency_key": str(idempotency_key_value),
            "action": "register_new_run",
        },
    )
    return True


def _transfer_no_of(row: Any) -> str | None:
    """从 dict / 领域对象 / 带 to_dict 的对象中提取转科单号。"""
    if isinstance(row, dict):
        value = row.get("transfer_no")
    elif hasattr(row, "transfer_no"):
        value = getattr(row, "transfer_no")
    elif hasattr(row, "to_dict"):
        value = row.to_dict().get("transfer_no")
    else:
        value = None
    if value is None:
        return None
    return str(value)


def dedupe_import_batch(rows: Iterable[Any]) -> tuple[list, list[dict]]:
    """导入批次内按 transfer_no 去重，只保留首条。

    返回 (保留行列表, 丢弃原因列表)。每个丢弃项含 transfer_no / reason /
    row_index / duplicate_of（首条所在行号），便于记录丢弃原因与条数。
    无转科单号的行无法判定重复，原样保留。
    """
    kept: list = []
    drops: list[dict] = []
    first_seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        transfer_no = _transfer_no_of(row)
        if transfer_no is None:
            kept.append(row)
            continue
        if transfer_no in first_seen:
            drops.append({
                "transfer_no": transfer_no,
                "reason": DUPLICATE_TRANSFER_NO_REASON,
                "row_index": index,
                "duplicate_of": first_seen[transfer_no],
            })
            continue
        first_seen[transfer_no] = index
        kept.append(row)
    return kept, drops


def record_drop(conn: sqlite3.Connection, transfer_no: str, reason: str) -> int:
    """把被丢弃的重复转科单行写入 import_drops 表（含原因，条数可 COUNT 查询）。"""
    cur = conn.execute(
        "INSERT INTO import_drops(transfer_no, reason, created_at) VALUES (?, ?, ?)",
        (str(transfer_no), str(reason), now_iso()),
    )
    conn.commit()
    append_audit(
        conn,
        run_id="import",
        node_id=str(transfer_no),
        event_type="import_drop",
        node_type="graph",
        actor="dedupe_import_batch",
        payload={"transfer_no": str(transfer_no), "reason": str(reason)},
    )
    return int(cur.lastrowid)
