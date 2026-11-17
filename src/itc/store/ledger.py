"""处置台账（只读筛选 + 脱敏导出）与只增不改的审计哈希链。

审计表只有追加入口 append_audit_event；本模块不提供任何改写 / 删除审计记录的
函数，底座 db.py 同样不给审计表开修改口子。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from itc.store.db import append_audit, chain_hash, fetch_dispositions, now_iso

_AUDIT_TABLES = frozenset({"audit_events", "execution_events"})
_LEDGER_FILTERS = frozenset({
    "transfer_no", "state", "execute_dept_code", "run_id",
    "from_ts", "to_ts", "inpatient_no", "kind", "limit",
})
_DB_FILTERS = frozenset({"transfer_no", "state", "execute_dept_code", "run_id", "from_ts", "to_ts"})
# 调用方可能用不同的键名表达同一筛选条件，统一归一化。
_FILTER_ALIASES = {
    "dept": "execute_dept_code", "dept_code": "execute_dept_code",
    "department": "execute_dept_code", "status": "state",
    "from_date": "from_ts", "to_date": "to_ts",
    "start_ts": "from_ts", "end_ts": "to_ts",
}

# 姓名 / 身份证 / 联系方式等键一律不落台账与审计导出。
_PHI_KEYS = frozenset({
    "name", "patient_name", "姓名", "患者姓名",
    "id_no", "id_card", "id_number", "identity_no", "identity_card", "身份证", "身份证号",
    "phone", "phone_no", "mobile", "tel", "telephone", "contact", "contact_no",
    "联系方式", "电话", "联系电话", "手机",
    "address", "home_address", "住址", "家庭住址",
})
_INPATIENT_KEYS = frozenset({"inpatient_no", "inpatientno", "住院号", "住院号原文"})


def digest_inpatient_no(value) -> str:
    """住院号只留摘要（sha256 前 12 位）。"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _is_phi(key) -> bool:
    return str(key).lower() in _PHI_KEYS


def _is_inpatient(key) -> bool:
    return str(key).lower() in _INPATIENT_KEYS


def redact_record(record):
    """递归脱敏：住院号摘要化、PHI 键剔除；JSON 字符串形式的载荷同样处理。"""
    if isinstance(record, list):
        return [redact_record(x) for x in record]
    if isinstance(record, str):
        try:
            parsed = json.loads(record)
        except (TypeError, ValueError):
            return record
        if isinstance(parsed, (dict, list)):
            return json.dumps(redact_record(parsed), ensure_ascii=False, sort_keys=True)
        return record
    if not isinstance(record, dict):
        return record
    out = {}
    for key, value in record.items():
        if _is_phi(key):
            continue
        if _is_inpatient(key):
            out[key] = digest_inpatient_no(value)
        else:
            out[key] = redact_record(value)
    return out


def _redact_payload(payload):
    """审计事件入链前脱敏；保持值为普通类型，避免出现二次 JSON 编码。"""
    if not isinstance(payload, dict):
        return payload
    out = {}
    for key, value in payload.items():
        if _is_phi(key):
            continue
        if _is_inpatient(key):
            out[key] = digest_inpatient_no(value)
        elif isinstance(value, dict):
            out[key] = _redact_payload(value)
        elif isinstance(value, list):
            out[key] = [_redact_payload(x) if isinstance(x, dict) else x for x in value]
        else:
            out[key] = value
    return out


def query_ledger(conn, **filters) -> list[dict]:
    """只读台账：按转科单号 / 状态 / 科室 / 时间范围（及住院号、类型）筛选。"""
    normalized = {_FILTER_ALIASES.get(k, k): v for k, v in filters.items()}
    bad = set(normalized) - _LEDGER_FILTERS
    if bad:
        raise ValueError(f"不支持的台账筛选项：{sorted(bad)}")
    rows = fetch_dispositions(
        conn, **{k: v for k, v in normalized.items() if k in _DB_FILTERS and v is not None}
    )
    for key in ("inpatient_no", "kind"):
        if normalized.get(key) is not None:
            rows = [r for r in rows if r.get(key) == normalized[key]]
    limit = normalized.get("limit")
    if limit is not None:
        rows = rows[:int(limit)]
    return rows


def _write_json(path, data) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def export_ledger(conn, path, **filters) -> Path:
    """台账导出（脱敏）：住院号只留摘要，姓名 / 证件 / 联系方式不落文件。"""
    rows = query_ledger(conn, **filters)
    return _write_json(path, [redact_record(r) for r in rows])


def export_audit(conn, path, *, table: str = "audit_events") -> Path:
    """审计链导出（脱敏）：保留 seq / prev_hash / hash 供内审校验，载荷脱敏。"""
    _check_table(table)
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM {table} ORDER BY seq ASC"
    ).fetchall()]
    return _write_json(path, [redact_record(r) for r in rows])


def append_audit_event(conn, *, run_id: str, node_id: str, event_type: str,
                       node_type: str = "", actor: str = "", payload: dict | None = None,
                       table: str = "audit_events") -> str:
    """只增不改：追加一条审计事件并返回本条哈希。载荷先脱敏再入链。"""
    _check_table(table)
    return append_audit(
        conn,
        run_id=run_id,
        node_id=node_id,
        event_type=event_type,
        node_type=node_type,
        actor=actor,
        payload=_redact_payload(payload or {}),
        table=table,
    )


def _check_table(table: str) -> None:
    if table not in _AUDIT_TABLES:
        raise ValueError(f"审计链表名白名单：{sorted(_AUDIT_TABLES)}")


def chain_length(conn, table: str = "audit_events") -> int:
    _check_table(table)
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def verify_audit_chain_detailed(conn, table: str = "audit_events") -> dict:
    """逐条重算哈希链，返回断点位置与断链条数。

    每条记录含前一条哈希（prev_hash）；任一条被改写后，其自身哈希重算不符，
    并会牵连后续记录的前驱校验。break_at 为首处断点的 seq。
    """
    _check_table(table)
    payload_col = "payload" if table == "audit_events" else "result_summary"
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM {table} ORDER BY seq ASC"
    ).fetchall()]
    prev = "0" * 64
    breaks: list[dict] = []
    for index, row in enumerate(rows, start=1):
        try:
            payload = json.loads(row.get(payload_col) or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        expected = chain_hash(prev, {
            "event_type": row["event_type"],
            "node_id": row["node_id"],
            **payload,
        })
        if row.get("prev_hash") != prev or row.get("hash") != expected:
            breaks.append({"seq": row["seq"], "index": index})
        prev = row["hash"]
    return {
        "ok": not breaks,
        "break_at": breaks[0]["seq"] if breaks else None,
        "break_index": breaks[0]["index"] if breaks else None,
        "broken_count": len(breaks),
        "breaks": breaks,
        "chain_length": len(rows),
        "table": table,
        "verified_at": now_iso(),
    }


def verify_audit_chain(conn, table: str = "audit_events") -> tuple[bool, int | None]:
    """返回 (链是否完整, 首处断点 seq 或 None)。"""
    detail = verify_audit_chain_detailed(conn, table)
    return detail["ok"], detail["break_at"]


def broken_chain_count(conn, table: str = "audit_events") -> int:
    """断链条数（被判定不连续的记录条数）。"""
    return verify_audit_chain_detailed(conn, table)["broken_count"]
