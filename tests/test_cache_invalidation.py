"""缓存失效传播：沿连线递归标脏、受影响集合、未受影响分支保持命中、传播终止。"""
from __future__ import annotations

from itc.graph.cache import CacheStore, cache_key
from itc.graph.external_block import ExternalBlockStore


def _chain_graph():
    # A -> B -> C；独立分支 D -> E
    return {
        "A": {"class_type": "fetch_orders", "inputs": {}},
        "B": {"class_type": "rule_judge", "inputs": {"orders": ["A", 0]}},
        "C": {"class_type": "change_site", "inputs": {"decision": ["B", 0]}},
        "D": {"class_type": "fetch_charges", "inputs": {}},
        "E": {"class_type": "bed_fee_hint", "inputs": {"charges": ["D", 0]}},
    }


def _seed(cs, nodes):
    for nid in nodes:
        cs.set(cache_key(nid, "h"), node_id=nid, input_hash="h", output={},
               summary="", source="judge", run_id="r1")


def test_invalidate_marks_recursive_dirty_and_returns_affected(db_conn):
    cs = CacheStore(db_conn)
    graph = _chain_graph()
    _seed(cs, graph.keys())

    affected = cs.invalidate("A", graph=graph, run_id="r1")
    # 沿 A->B->C 递归标脏
    assert affected == {"A", "B", "C"}
    assert cs.affected_set() == {"A", "B", "C"}
    # 确实写进 cache_dirty 表
    rows = db_conn.execute(
        "SELECT node_id FROM cache_dirty WHERE run_id='r1'"
    ).fetchall()
    assert {r["node_id"] for r in rows} == {"A", "B", "C"}


def test_unaffected_branch_stays_cached(db_conn):
    cs = CacheStore(db_conn)
    graph = _chain_graph()
    _seed(cs, graph.keys())

    cs.invalidate("A", graph=graph, run_id="r1")
    # 未受影响的独立分支 D、E 保持命中
    assert cs.is_cached(cache_key("D", "h"), "h") is True
    assert cs.is_cached(cache_key("E", "h"), "h") is True
    # 受影响分支不再命中（脏标记与命中隔离）
    assert cs.is_cached(cache_key("A", "h"), "h") is False
    assert cs.is_cached(cache_key("B", "h"), "h") is False
    assert cs.is_cached(cache_key("C", "h"), "h") is False


def test_propagation_stops_at_missing_downstream(db_conn):
    cs = CacheStore(db_conn)
    # 显式 links 指向不存在的节点 Z：传播到边界即终止
    graph = {
        "nodes": {
            "A": {"class_type": "fetch", "inputs": {}},
            "B": {"class_type": "judge", "inputs": {}},
        },
        "links": [{"from_node_id": "A", "to_node_id": "B"},
                  {"from_node_id": "B", "to_node_id": "GHOST"}],
    }
    from itc.graph.cache import _graph_nodes
    _seed(cs, _graph_nodes(graph).keys())
    affected = cs.invalidate("A", graph=graph, run_id="r1")
    assert "GHOST" not in affected
    assert affected == {"A", "B"}


def test_propagation_stops_at_blocked_node(db_conn):
    cs = CacheStore(db_conn)
    graph = _chain_graph()
    # 把链路延长：C -> F
    graph["F"] = {"class_type": "writeback", "inputs": {"x": ["C", 0]}}
    _seed(cs, graph.keys())
    # C 节点已有 pending 外部阻塞（同一 run）
    ExternalBlockStore(db_conn).create("r1", "C", {"reason": "需医师判断"})

    affected = cs.invalidate("A", graph=graph, run_id="r1")
    # 传播到 C 之前终止：C、F 不入受影响集合
    assert "C" not in affected
    assert "F" not in affected
    assert affected == {"A", "B"}
    # 边界之外仍保持命中
    assert cs.is_cached(cache_key("F", "h"), "h") is True
