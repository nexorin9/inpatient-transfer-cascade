"""运行回放：只读、不发写请求、摘要字段齐全带回放标注、可落盘脱敏产物。"""
from __future__ import annotations

import json

from itc.graph.engine import Engine
from itc.graph.replay import replay_run, write_replay_summary


def _graph():
    return {
        "A": {"class_type": "fetch_orders", "source": "fetch", "inputs": {}},
        "B": {"class_type": "change_site", "source": "writeback",
              "inputs": {"order_no": "O-1", "execute_dept_code": "D1",
                         "effective_from": "2026-01-01"}},
    }


def _ok(node_id, info, run_id, transfer_no):
    return {"status": "ok", "output": {"n": node_id}, "summary": "ok-" + node_id,
            "source": info.get("source", "")}


def _snapshot(conn):
    tables = (
        "graph_runs", "execution_events", "audit_events", "cache_entries",
        "cache_dirty", "writeback_registry", "dispositions", "external_blocks",
    )
    return {
        t: conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
        for t in tables
    }


def test_replay_is_read_only_and_no_write_requests(db_conn):
    e = Engine(db_conn, executor=_ok)
    e.submit("rr", _graph(), transfer_no="T-1")
    e.run("rr")

    before = _snapshot(db_conn)
    summary = replay_run(db_conn, "rr")
    after = _snapshot(db_conn)
    # 只读：任何表都没新增 / 改动行，未触发任何写请求
    assert before == after


def test_replay_summary_fields_complete_and_marked(db_conn):
    e = Engine(db_conn, executor=_ok)
    e.submit("rr", _graph(), transfer_no="T-1")
    e.run("rr")
    s = replay_run(db_conn, "rr")

    assert s["replay"] is True
    assert "重放" in s["replay_note"] or "回放" in s["replay_note"]
    for field in ("nodes", "duration_ms", "cache_hits", "blocks",
                  "writebacks", "manual_items"):
        assert field in s
    assert s["nodes"] == 2
    assert s["writebacks"] == 1          # 一次写回埋点
    assert s["cache_hits"] == 0          # 首跑无命中
    assert s["run_id"] == "rr"
    assert s["status"] == "success"


def test_replay_counts_reflect_events_across_runs(db_conn):
    e = Engine(db_conn, executor=_ok)
    g = _graph()
    e.submit("r1", g, transfer_no="T-1")
    e.run("r1")
    # 相同业务哈希（不含 run_id）-> 二次运行命中缓存
    e.submit("r2", g, transfer_no="T-1")
    e.run("r2")

    s1 = replay_run(db_conn, "r1")
    s2 = replay_run(db_conn, "r2")
    assert s1["writebacks"] >= 1
    assert s2["cache_hits"] >= 1          # 命中数来自 cache_hit 事件
    assert s2["replay"] is True and s1["replay"] is True


def test_write_replay_summary_is_redacted_file(db_conn, tmp_path):
    s = replay_run(db_conn, "no-such-run")
    # 注入 PHI 字段，验证产物脱敏
    s["inpatient_no"] = "1234567"
    s["nested"] = {"name": "张三", "phone": "13800000000", "safe": "keep"}
    out = tmp_path / "outputs" / "replay_summary.json"
    p = write_replay_summary(s, out)
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["inpatient_no"] == "[REDACTED]"
    assert data["nested"]["name"] == "[REDACTED]"
    assert data["nested"]["phone"] == "[REDACTED]"
    assert data["nested"]["safe"] == "keep"
    assert data["replay"] is True
