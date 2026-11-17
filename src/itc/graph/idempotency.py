"""业务字段幂等键：只由四个业务字段决定，与时间戳、重试次数无关。

对应写回请求头 Idempotency-Key = sha256(transfer_no|order_no|execute_dept_code|effective_from)。
"""
from __future__ import annotations

import hashlib
import json

# 易变字段不参与业务哈希，保证重算结果稳定
_VOLATILE_FIELDS = {
    "timestamp", "ts", "retries", "retry", "attempt", "attempts",
    "run_id", "created_at", "updated_at", "now",
}


def idempotency_key(transfer_no, order_no, execute_dept_code, effective_from):
    """sha256(transfer_no|order_no|execute_dept_code|effective_from)。"""
    raw = f"{transfer_no}|{order_no}|{execute_dept_code}|{effective_from}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def business_hash(**fields):
    """对业务字段做稳定哈希；过滤时间戳 / 重试次数等易变字段。"""
    stable = {k: v for k, v in fields.items() if k not in _VOLATILE_FIELDS}
    body = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
