"""落盘缓存基本读写：条目记录产出摘要 / 状态 / 写入时间 / 来源；输入哈希一致才命中。"""
from __future__ import annotations

from itc.graph.cache import CacheStore, cache_key


def test_set_get_records_full_entry(db_conn):
    cs = CacheStore(db_conn)
    key = cache_key("A:fetch_orders", "biz-hash-1")
    cs.set(
        key, node_id="A", input_hash="biz-hash-1",
        output={"orders": [1, 2]}, summary="取到 2 条医嘱",
        status="ok", source="fetch", run_id="run-1",
    )
    row = cs.get(key)
    assert row is not None
    assert row["node_id"] == "A"
    assert row["input_hash"] == "biz-hash-1"
    assert row["output"] == {"orders": [1, 2]}
    assert row["output_summary"] == "取到 2 条医嘱"
    assert row["status"] == "ok"
    assert row["source"] == "fetch"       # 来源（取数 / 判定 / 写回）
    assert row["run_id"] == "run-1"
    assert row["created_at"]              # 写入时间


def test_is_cached_requires_matching_input_hash(db_conn):
    cs = CacheStore(db_conn)
    key = cache_key("A:fetch_orders", "biz-hash-1")
    cs.set(key, node_id="A", input_hash="biz-hash-1", output={"v": 1}, source="fetch")
    assert cs.is_cached(key, "biz-hash-1") is True
    assert cs.is_cached(key, "biz-hash-2") is False
    assert cs.is_cached(cache_key("A:fetch_orders", "other"), "biz-hash-1") is False


def test_summary_counts_by_source_and_status(db_conn):
    cs = CacheStore(db_conn)
    cs.set(cache_key("A", "h1"), node_id="A", input_hash="h1", output={}, source="fetch")
    cs.set(cache_key("B", "h2"), node_id="B", input_hash="h2", output={}, source="judge")
    cs.set(cache_key("C", "h3"), node_id="C", input_hash="h3", output={},
           source="writeback", status="degraded")
    s = cs.summary()
    assert s["entries"] == 3
    assert s["by_source"]["fetch"] == 1
    assert s["by_source"]["judge"] == 1
    assert s["by_source"]["writeback"] == 1
    assert s["by_status"]["degraded"] == 1
    assert s["dirty"] == 0


def test_cached_entry_status_is_queryable(db_conn):
    cs = CacheStore(db_conn)
    key = cache_key("B:model_judge", "bh")
    cs.set(key, node_id="B", input_hash="bh", output={}, status="UNDETERMINED",
           source="judge", summary="待人工判定")
    row = cs.get(key)
    assert row["status"] == "UNDETERMINED"
    assert row["output_summary"] == "待人工判定"
    assert cs.is_cached(key, "bh") is True
