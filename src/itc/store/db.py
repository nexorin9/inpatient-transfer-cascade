"""本地 SQLite 单文件状态：连接、schema 建表、幂等迁移与只增不改写入口。

所有模块共用本文件的连接与表结构；审计表只提供 append，不提供任何修改入口。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_runs (
    run_id TEXT PRIMARY KEY,
    transfer_no TEXT NOT NULL,
    template_id TEXT,
    template_version TEXT,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS dispositions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    transfer_no TEXT NOT NULL,
    inpatient_no TEXT NOT NULL,
    order_no TEXT,
    specimen_no TEXT,
    charge_ref TEXT,
    kind TEXT NOT NULL,
    action TEXT,
    judgement TEXT,
    state TEXT NOT NULL DEFAULT 'pending_fetch',
    confidence REAL DEFAULT 0,
    evidence_fields TEXT,
    evidence_snippet TEXT,
    reasoning_summary TEXT,
    stop_draft_content TEXT,
    execute_dept_code TEXT,
    execute_dept_name TEXT,
    effective_from TEXT,
    idempotency_key TEXT,
    rule_hits TEXT,
    manual_reason TEXT,
    degraded INTEGER NOT NULL DEFAULT 0,
    degraded_reason TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    role TEXT,
    audit_hash TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS external_blocks (
    block_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    context TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT,
    released_at TEXT,
    released_by TEXT
);
CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    run_id TEXT,
    input_hash TEXT NOT NULL,
    output_summary TEXT,
    output_json TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    source TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS cache_dirty (
    node_id TEXT NOT NULL,
    run_id TEXT,
    marked_at TEXT,
    PRIMARY KEY (node_id, run_id)
);
CREATE TABLE IF NOT EXISTS writeback_registry (
    idempotency_key TEXT PRIMARY KEY,
    transfer_no TEXT NOT NULL,
    order_no TEXT NOT NULL,
    status_code INTEGER,
    response_digest TEXT,
    disposition_id INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS pending_writebacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disposition_id INTEGER,
    idempotency_key TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    created_at TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    node_id TEXT,
    node_type TEXT,
    event_type TEXT NOT NULL,
    actor TEXT,
    payload TEXT,
    prev_hash TEXT,
    hash TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS execution_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_type TEXT,
    event_type TEXT NOT NULL,
    duration_ms REAL,
    result_summary TEXT,
    prev_hash TEXT,
    hash TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS model_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT,
    latency_ms REAL,
    input_digest TEXT,
    judgement TEXT,
    degraded INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS graph_idempotency (
    transfer_no TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS import_drops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_no TEXT,
    reason TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dispositions_transfer ON dispositions(transfer_no);
CREATE INDEX IF NOT EXISTS idx_dispositions_state ON dispositions(state);
CREATE INDEX IF NOT EXISTS idx_execution_events_run ON execution_events(run_id);
"""

_conn_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）状态库；多线程工作线程可共用同一连接。"""
    path = Path(os.path.expanduser(str(db_path)))
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> int:
    """建表；可重复执行。返回当前 schema 版本。"""
    with _conn_lock:
        conn.executescript(_DDL)
        row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    return SCHEMA_VERSION


def migrate(conn: sqlite3.Connection) -> int:
    """版本迁移（幂等）：连跑两次结果一致。"""
    init_schema(conn)
    with _conn_lock:
        row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        version = int(row["version"]) if row else 0
        if version < SCHEMA_VERSION:
            conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
            conn.commit()
        return SCHEMA_VERSION


def chain_hash(prev_hash: str, payload: dict | str) -> str:
    """审计哈希链：sha256(前一条哈希 | 本条规范化载荷)。"""
    body = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{prev_hash}|{body}".encode("utf-8")).hexdigest()


def last_hash(conn: sqlite3.Connection, table: str = "audit_events") -> str:
    if table not in ("audit_events", "execution_events"):
        raise ValueError("只允许查询审计链与执行事件链")
    row = conn.execute(f"SELECT hash FROM {table} ORDER BY seq DESC LIMIT 1").fetchone()
    return row["hash"] if row else "0" * 64


def append_audit(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    node_id: str,
    event_type: str,
    node_type: str = "",
    actor: str = "",
    payload: dict | None = None,
    table: str = "audit_events",
) -> str:
    """只增不改：追加一条审计/执行事件并返回本条哈希。"""
    if table not in ("audit_events", "execution_events"):
        raise ValueError("审计链表名白名单：audit_events / execution_events")
    payload = payload or {}
    with _conn_lock:
        prev = last_hash(conn, table)
        digest = chain_hash(prev, {"event_type": event_type, "node_id": node_id, **payload})
        if table == "audit_events":
            conn.execute(
                "INSERT INTO audit_events(run_id,node_id,node_type,event_type,actor,payload,prev_hash,hash,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, node_id, node_type, event_type, actor,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), prev, digest, now_iso()),
            )
        else:
            conn.execute(
                "INSERT INTO execution_events(run_id,node_id,node_type,event_type,duration_ms,result_summary,prev_hash,hash,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, node_id, node_type, event_type, float(payload.get("duration_ms", 0) or 0),
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), prev, digest, now_iso()),
            )
        conn.commit()
    return digest


def insert_disposition(conn: sqlite3.Connection, item) -> int:
    """把 DispositionItem 落库，返回主键。"""
    d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
    fields = {
        "run_id": d.get("run_id", ""),
        "transfer_no": d["transfer_no"],
        "inpatient_no": d["inpatient_no"],
        "order_no": d.get("order_no") or (d.get("ref") if d.get("kind") == "order" else ""),
        "specimen_no": d.get("specimen_no") or (d.get("ref") if d.get("kind") == "specimen" else ""),
        "charge_ref": d.get("ref") if d.get("kind") == "charge" else "",
        "kind": d["kind"],
        "action": d.get("action", ""),
        "judgement": d.get("judgement", "UNDETERMINED"),
        "state": d.get("state", "pending_fetch"),
        "confidence": d.get("confidence", 0.0),
        "evidence_fields": json.dumps(d.get("evidence_fields", []), ensure_ascii=False),
        "evidence_snippet": d.get("evidence_snippet", ""),
        "reasoning_summary": d.get("reasoning_summary", ""),
        "stop_draft_content": d.get("stop_draft_content", ""),
        "execute_dept_code": d.get("execute_dept_code", ""),
        "execute_dept_name": d.get("execute_dept_name", ""),
        "effective_from": d.get("effective_from", ""),
        "idempotency_key": d.get("idempotency_key", ""),
        "rule_hits": json.dumps(d.get("rule_hits", []), ensure_ascii=False),
        "manual_reason": d.get("manual_reason", ""),
        "degraded": int(bool(d.get("degraded", False))),
        "degraded_reason": d.get("degraded_reason", ""),
        "confirmed_by": d.get("confirmed_by", ""),
        "role": d.get("role", ""),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    cols = ",".join(fields)
    marks = ",".join("?" for _ in fields)
    with _conn_lock:
        cur = conn.execute(f"INSERT INTO dispositions({cols}) VALUES ({marks})", tuple(fields.values()))
        conn.commit()
        return int(cur.lastrowid)


def update_disposition(conn: sqlite3.Connection, item_id: int, **changes) -> None:
    """更新处置项（审计表无此入口）。状态迁移校验由 store/state_machine.py 负责。"""
    if not changes:
        return
    allowed = {
        "state", "action", "judgement", "confidence", "evidence_fields", "evidence_snippet",
        "reasoning_summary", "stop_draft_content", "execute_dept_code", "execute_dept_name",
        "effective_from", "idempotency_key", "rule_hits", "manual_reason", "degraded",
        "degraded_reason", "confirmed_by", "confirmed_at", "role", "audit_hash", "run_id",
    }
    bad = set(changes) - allowed
    if bad:
        raise ValueError(f"不允许更新的字段：{sorted(bad)}")
    values = []
    for key, value in changes.items():
        if key in ("evidence_fields", "rule_hits") and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        if key in ("degraded",):
            value = int(bool(value))
        values.append(value)
    assignments = ",".join(f"{k}=?" for k in changes)
    with _conn_lock:
        conn.execute(
            f"UPDATE dispositions SET {assignments}, updated_at=? WHERE id=?",
            (*values, now_iso(), item_id),
        )
        conn.commit()


def fetch_dispositions(conn: sqlite3.Connection, **filters):
    """只读查询：按转科单号 / 状态 / 科室 / 时间范围筛选。"""
    sql = "SELECT * FROM dispositions WHERE 1=1"
    params: list = []
    for key in ("transfer_no", "state", "execute_dept_code", "run_id"):
        if key in filters and filters[key] is not None:
            sql += f" AND {key}=?"
            params.append(filters[key])
    if filters.get("from_ts"):
        sql += " AND created_at >= ?"
        params.append(filters["from_ts"])
    if filters.get("to_ts"):
        sql += " AND created_at <= ?"
        params.append(filters["to_ts"])
    sql += " ORDER BY id ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
