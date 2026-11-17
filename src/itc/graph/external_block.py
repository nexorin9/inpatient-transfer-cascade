"""持久化外部阻塞（中途等人）：阻塞落 SQLite，解除写审计事件，进程重启可恢复。

对应 ComfyUI comfy_execution/graph.py:168 add_external_block() 与 unblock() 闭包、
:247-251 等待解除。本轮把内存 asyncio.Event 换成 SQLite 持久化：
图停在「要医师判断那一步」等人，进程重启后停在同一节点等待确认。
"""
from __future__ import annotations

import json
import threading
import uuid

from itc.store.db import append_audit, now_iso


class ExternalBlockStore:
    """external_blocks 表的读写入口；解除动作同时写一条审计事件（谁 / 何时 / 哪个阻塞）。"""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._lock = threading.RLock()

    def create(self, run_id, node_id, context) -> str:
        """登记一个待确认阻塞，返回 block_id。context 允许 dict 或字符串。"""
        block_id = f"blk-{uuid.uuid4().hex[:16]}"
        ctx = (
            context
            if isinstance(context, str)
            else json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
        )
        with self._lock:
            self.conn.execute(
                "INSERT INTO external_blocks"
                "(block_id,run_id,node_id,context,status,created_at,released_at,released_by)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (block_id, run_id, node_id, ctx, "pending", now_iso(), None, None),
            )
            self.conn.commit()
            append_audit(
                self.conn,
                run_id=run_id,
                node_id=node_id,
                event_type="external_block_create",
                payload={"block_id": block_id, "node_id": node_id, "status": "pending"},
                table="audit_events",
            )
        return block_id

    def release(self, block_id, actor="") -> dict:
        """人工确认后解除阻塞：更新状态并写审计事件（操作人 / 时间 / 阻塞 id）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT block_id,run_id,node_id,status FROM external_blocks WHERE block_id=?",
                (block_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"外部阻塞不存在：{block_id}")
            released_at = now_iso()
            self.conn.execute(
                "UPDATE external_blocks SET status='released',released_at=?,released_by=? WHERE block_id=?",
                (released_at, actor, block_id),
            )
            self.conn.commit()
            append_audit(
                self.conn,
                run_id=row["run_id"],
                node_id=row["node_id"],
                event_type="external_block_release",
                actor=actor,
                payload={
                    "block_id": block_id,
                    "node_id": row["node_id"],
                    "released_by": actor,
                    "released_at": released_at,
                },
                table="audit_events",
            )
            return {
                "block_id": block_id,
                "run_id": row["run_id"],
                "node_id": row["node_id"],
                "status": "released",
                "released_at": released_at,
                "released_by": actor,
            }

    def _rows(self, run_id=None, only_pending=True) -> list[dict]:
        sql = (
            "SELECT block_id,run_id,node_id,context,status,created_at,released_at,released_by "
            "FROM external_blocks"
        )
        conds = []
        params: list = []
        if run_id is not None:
            conds.append("run_id=?")
            params.append(run_id)
        if only_pending:
            conds.append("status='pending'")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at ASC, block_id ASC"
        return [self._to_dict(r) for r in self.conn.execute(sql, params).fetchall()]

    @staticmethod
    def _to_dict(r) -> dict:
        d = dict(r)
        ctx = d.get("context")
        if isinstance(ctx, str):
            try:
                d["context"] = json.loads(ctx)
            except (ValueError, TypeError):
                pass
        return d

    def pending(self, run_id=None) -> list[dict]:
        """列出仍处于 pending 的阻塞；run_id 为空则跨 run 查询。"""
        return self._rows(run_id=run_id, only_pending=True)

    def restore(self, run_id) -> list[dict]:
        """进程重启恢复：重新取出该 run 仍待确认的阻塞，停在同一节点等待确认。"""
        return self._rows(run_id=run_id, only_pending=True)

    def blocked_node_ids(self, run_id=None) -> set[str]:
        """执行主循环用：当前生效的受阻节点 id 集合。"""
        return {r["node_id"] for r in self._rows(run_id=run_id, only_pending=True)}
