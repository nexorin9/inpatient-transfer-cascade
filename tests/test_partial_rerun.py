"""局部重跑：受影响分支重入队、取数节点不增调用、台账追加、写回同一幂等键。"""
from __future__ import annotations

from itc.graph.engine import Engine, idempotency_key


class CountingReader:
    """注入的计数 reader：命中缓存的取数节点不会调用它，可直接度量 HIS 请求次数。"""

    def __init__(self):
        self.calls = 0

    def read(self, *args, **kwargs):
        self.calls += 1
        return []


def _graph():
    # F1 -> J -> W；独立分支 F2 -> H
    return {
        "F1": {"class_type": "fetch_orders", "source": "fetch",
               "inputs": {"inpatient_no": "1001"}},
        "F2": {"class_type": "fetch_charges", "source": "fetch",
               "inputs": {"inpatient_no": "1001"}},
        "J": {"class_type": "rule_judge", "source": "judge",
              "inputs": {"orders": ["F1", 0]}},
        "H": {"class_type": "bed_fee_hint", "source": "judge",
              "inputs": {"charges": ["F2", 0]}},
        "W": {"class_type": "change_site", "source": "writeback",
              "inputs": {"decision": ["J", 0], "order_no": "O-1",
                         "execute_dept_code": "D1", "effective_from": "2026-01-01"}},
    }


def test_partial_rerun_skips_cached_fetch_and_appends_ledger(db_conn):
    reader = CountingReader()
    write_calls = []

    def executor(node_id, info, run_id, transfer_no):
        src = info.get("source", "")
        if src == "fetch":
            reader.read()
        if src == "writeback":
            write_calls.append((run_id, node_id))
        return {"status": "ok", "output": {"n": node_id},
                "summary": "ok-" + node_id, "source": src}

    eng = Engine(db_conn, executor=executor)
    graph = _graph()

    # 首跑：全部取数 / 判定 / 写回
    assert eng.submit("run1", graph, transfer_no="T-1") is True
    r1 = eng.run("run1")
    assert r1.status == "success"
    first_reads = reader.calls
    first_writes = len(write_calls)
    assert first_reads == 2       # F1、F2 各一次
    assert first_writes == 1      # W 写回一次

    run1_events = db_conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE run_id='run1'"
    ).fetchone()[0]
    runs_before = db_conn.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]

    # 护士长改一条处置项 J：失效传播只影响 J 及其下游 W
    affected = eng.cache.invalidate("J", graph=graph, run_id="run2")
    assert affected == {"J", "W"}
    # 未受影响的取数分支仍命中
    from itc.graph.cache import cache_key
    from itc.graph.engine import _input_hash
    ih_f1 = _input_hash("F1", graph["F1"], "T-1")
    assert eng.cache.is_cached(cache_key("F1:fetch_orders", ih_f1), ih_f1) is True

    # 局部重跑：新 run_id 追加台账
    r2 = eng.partial_rerun("run2", graph, transfer_no="T-1", affected=affected)
    assert r2.status == "success"

    # 命中缓存的取数节点不发起 HIS 请求：调用次数不增加
    assert reader.calls == first_reads
    # 重跑写回仍走同一幂等键，不产生第二次写请求
    assert len(write_calls) == first_writes

    # 台账追加而非覆盖
    runs_after = db_conn.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]
    assert runs_after == runs_before + 1
    assert db_conn.execute(
        "SELECT status FROM graph_runs WHERE run_id='run1'"
    ).fetchone()[0] == "success"          # 原记录仍在、未被覆盖
    assert db_conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE run_id='run1'"
    ).fetchone()[0] == run1_events        # 原执行事件未被改动

    # 幂等键稳定且只有一条登记
    expected_key = idempotency_key("T-1", "O-1", "D1", "2026-01-01")
    rows = db_conn.execute("SELECT idempotency_key FROM writeback_registry").fetchall()
    assert [r["idempotency_key"] for r in rows] == [expected_key]


def test_idempotency_key_stable_and_input_sensitive():
    a = idempotency_key("T-1", "O-1", "D1", "2026-01-01")
    b = idempotency_key("T-1", "O-1", "D1", "2026-01-01")
    c = idempotency_key("T-1", "O-1", "D1", "2026-02-02")
    assert a == b                 # 同业务字段两次相等
    assert a != c                 # 改变 effective_from 则不等
    assert isinstance(a, str) and len(a) == 64
