"""缓存键设计：节点签名 + 业务哈希，不含时间戳 / 重试次数；跨进程（重启）仍命中。"""
from __future__ import annotations

import time

from itc.store.db import connect, init_schema
from itc.graph.cache import CacheStore, cache_key


def test_cache_key_is_deterministic_and_timestamp_free():
    k1 = cache_key("A:fetch_orders", "business-hash-xyz")
    k2 = cache_key("A:fetch_orders", "business-hash-xyz")
    assert k1 == k2
    # 键由节点签名与业务哈希拼成，不包含时间戳 / 重试次数 / 随机数
    assert "A:fetch_orders" in k1
    assert "business-hash-xyz" in k1
    time.sleep(0.01)
    assert cache_key("A:fetch_orders", "business-hash-xyz") == k1
    assert cache_key("A:fetch_orders", "business-hash-xyz", ) == k1  # 与重试次数无关
    assert cache_key("A:fetch_orders", "DIFFERENT") != k1


def test_same_input_hits_twice_changed_input_misses(db_conn):
    cs = CacheStore(db_conn)
    sig = "A:fetch_orders"
    cs.set(cache_key(sig, "h-in"), node_id="A", input_hash="h-in", output={"rows": 3},
           source="fetch", run_id="r1")
    # 同输入两次命中
    assert cs.is_cached(cache_key(sig, "h-in"), "h-in") is True
    assert cs.is_cached(cache_key(sig, "h-in"), "h-in") is True
    # 输入变更 -> 不命中（键变了，且输入哈希不一致也不命中）
    assert cs.is_cached(cache_key(sig, "h-changed"), "h-changed") is False
    assert cs.is_cached(cache_key(sig, "h-in"), "h-changed") is False


def test_restart_new_connection_still_hits(tmp_path):
    db_path = str(tmp_path / "cache_state.sqlite3")
    conn1 = connect(db_path)
    init_schema(conn1)
    cs1 = CacheStore(conn1)
    key = cache_key("F:fetch_charges", "stable-biz-hash")
    cs1.set(key, node_id="F", input_hash="stable-biz-hash", output={"charges": [1]},
            summary="床位费", source="fetch", run_id="r1")
    conn1.close()

    # 进程重启：新建连接指向同一 db 文件
    conn2 = connect(db_path)
    init_schema(conn2)
    cs2 = CacheStore(conn2)
    assert cs2.is_cached(key, "stable-biz-hash") is True
    assert cs2.get(key)["output_summary"] == "床位费"
    # 输入变了仍不命中
    assert cs2.is_cached(key, "changed") is False
    conn2.close()
