"""任务 5 / 33：业务字段幂等键（G1 实现，本组按契约 API 调用并做真实断言）。

覆盖：相同业务字段两次相等、改 effective_from 不等、与时间戳 / 重试次数无关。
"""
from __future__ import annotations

import time

from itc.graph.idempotency import business_hash, idempotency_key


def _is_sha256_hex(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def test_same_business_fields_equal():
    a = idempotency_key("T-001", "O-001", "DEPT-A", "2026-01-01T09:00:00+08:00")
    b = idempotency_key("T-001", "O-001", "DEPT-A", "2026-01-01T09:00:00+08:00")
    assert a == b
    assert _is_sha256_hex(a)


def test_change_effective_from_changes_key():
    base = idempotency_key("T-001", "O-001", "DEPT-A", "2026-01-01T09:00:00+08:00")
    changed = idempotency_key("T-001", "O-001", "DEPT-A", "2026-01-02T09:00:00+08:00")
    assert base != changed


def test_change_any_business_field_changes_key():
    base = idempotency_key("T-001", "O-001", "DEPT-A", "E")
    assert base != idempotency_key("T-002", "O-001", "DEPT-A", "E")
    assert base != idempotency_key("T-001", "O-002", "DEPT-A", "E")
    assert base != idempotency_key("T-001", "O-001", "DEPT-B", "E")


def test_independent_of_timestamp_and_retries():
    base = idempotency_key("T-001", "O-001", "DEPT-A", "E")
    time.sleep(0.002)  # 不同时刻重算
    assert idempotency_key("T-001", "O-001", "DEPT-A", "E") == base
    # 模拟多次重试上下文反复计算，取值恒定（幂等键不含时间戳 / 重试次数）
    for _attempt in range(3):
        assert idempotency_key("T-001", "O-001", "DEPT-A", "E") == base


def test_business_hash_deterministic_and_field_sensitive():
    assert business_hash(a=1, b=2) == business_hash(a=1, b=2)
    assert business_hash(a=1, b=2) != business_hash(a=1, b=3)
    assert isinstance(business_hash(x="y"), str) and business_hash(x="y")
