"""执行侧审计埋点：节点级事件、哈希链校验与断点定位。

对应 ComfyUI server.py:1045 GET /history 的运行历史语义。本模块只做「事件产生与校验」：
- 节点开始 / 结束 / 阻断 / 重试 / 写回 / 缓存命中 / 失败 / 人工确认各产生一条事件；
- 每条事件含节点 id、节点类型、run_id、耗时、结果摘要，并由底座串成哈希链；
- 哈希链只增不改（写入全部经 append_audit，本模块不提供任何 UPDATE/DELETE）。
"""
from __future__ import annotations

import json

from itc.store.db import append_audit, chain_hash

# 节点生命周期事件类型（引擎、回放、测试共用同一套取值）
EVENT_NODE_START = "node_start"
EVENT_NODE_END = "node_end"
EVENT_NODE_BLOCKED = "node_blocked"
EVENT_NODE_RETRY = "node_retry"
EVENT_WRITEBACK = "writeback"
EVENT_CACHE_HIT = "cache_hit"
EVENT_NODE_FAILED = "node_failed"
EVENT_MANUAL_CONFIRM = "manual_confirm"

NODE_EVENT_TYPES = (
    EVENT_NODE_START,
    EVENT_NODE_END,
    EVENT_NODE_BLOCKED,
    EVENT_NODE_RETRY,
    EVENT_WRITEBACK,
    EVENT_CACHE_HIT,
    EVENT_NODE_FAILED,
    EVENT_MANUAL_CONFIRM,
)

_VALID_TABLES = ("audit_events", "execution_events")


def emit_node_event(
    conn,
    run_id,
    node_id,
    *,
    event_type,
    node_type="",
    payload=None,
    actor="",
    table="execution_events",
):
    """追加一条节点级事件并返回本条哈希。

    actor（操作人）既写入 audit_events.actor 列，也写入载荷 JSON，
    因此无论落到哪张链，人工确认的操作人都可查、且被哈希链覆盖。
    duration_ms / result_summary 等字段由 payload 携带，经底座落 execution_events。
    """
    if table not in _VALID_TABLES:
        raise ValueError(f"事件链表名白名单：{_VALID_TABLES}")
    body = dict(payload or {})
    body.setdefault("node_type", node_type)
    body.setdefault("run_id", run_id)
    body.setdefault("node_id", node_id)
    if actor:
        body["actor"] = actor
    return append_audit(
        conn,
        run_id=run_id,
        node_id=node_id,
        event_type=event_type,
        node_type=node_type,
        actor=actor,
        payload=body,
        table=table,
    )


def chain_length(conn, table="execution_events") -> int:
    if table not in _VALID_TABLES:
        raise ValueError(f"事件链表名白名单：{_VALID_TABLES}")
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def _body_column(table: str) -> str:
    return "payload" if table == "audit_events" else "result_summary"


def verify_chain(conn, table="execution_events") -> tuple[bool, int | None]:
    """校验哈希链：逐条重算 hash 并核对 prev_hash 连续性。

    返回 (是否完整, 首个断点 seq)。篡改一条事件的载荷 / 哈希 / 类型，
    或删除中间一条，都会在此返回断点位置 seq（None 表示链完整）。
    """
    if table not in _VALID_TABLES:
        raise ValueError(f"事件链表名白名单：{_VALID_TABLES}")
    col = _body_column(table)
    rows = conn.execute(
        f"SELECT seq, run_id, node_id, event_type, {col} AS body, prev_hash, hash "
        f"FROM {table} ORDER BY seq ASC"
    ).fetchall()
    prev = "0" * 64
    for r in rows:
        try:
            body = json.loads(r["body"]) if r["body"] else {}
        except (ValueError, TypeError):
            return (False, int(r["seq"]))
        # 与底座 append_audit 的 hash 输入保持完全一致
        expected = chain_hash(
            prev,
            {"event_type": r["event_type"], "node_id": r["node_id"], **body},
        )
        if r["prev_hash"] != prev or r["hash"] != expected:
            return (False, int(r["seq"]))
        prev = r["hash"]
    return (True, None)
