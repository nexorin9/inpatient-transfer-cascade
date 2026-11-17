"""运行回放：按图运行 id 只读重放，产出运行摘要；绝不触发任何写请求。

摘要字段：节点数 / 耗时 / 缓存命中数 / 阻断数 / 写回数 / 待人工项数，
并带 replay=True 标注，与真实写回记录明确区分。可落盘为脱敏产物供交班与内审。
"""
from __future__ import annotations

import json
from pathlib import Path

from itc.store.db import now_iso

# 需要脱敏的键（命中即遮蔽），覆盖姓名 / 证件 / 联系方式 / 住院号原文
_PHI_KEYS = {
    "inpatient_no", "name", "patient_name", "id_no", "id_number", "id_card",
    "phone", "mobile", "tel", "contact", "address",
}

_SUMMARY_FIELDS = (
    "nodes", "duration_ms", "cache_hits", "blocks", "writebacks", "manual_items",
)


def _as_int(v, default=0) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def replay_run(conn, run_id) -> dict:
    """只读重放：仅 SELECT，不写库、不发任何 HIS / 写回请求。

    优先从 execution_events 逐条汇总；该 run 无执行事件时，
    退回读取 graph_runs.summary_json（引擎写入的运行摘要）。
    """
    row = conn.execute(
        "SELECT * FROM graph_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    run = dict(row) if row is not None else {}

    events = conn.execute(
        "SELECT node_id,event_type,duration_ms,result_summary "
        "FROM execution_events WHERE run_id=? ORDER BY seq ASC",
        (run_id,),
    ).fetchall()

    node_ids: set[str] = set()
    duration_ms = 0.0
    cache_hits = blocks = writebacks = manual_items = 0
    for r in events:
        node_ids.add(r["node_id"])
        duration_ms += float(r["duration_ms"] or 0)
        et = r["event_type"]
        if et == "cache_hit":
            cache_hits += 1
        elif et in ("node_blocked", "blocked"):
            blocks += 1
        elif et == "writeback":
            writebacks += 1
        body = {}
        try:
            body = json.loads(r["result_summary"] or "{}")
        except (ValueError, TypeError):
            body = {}
        if et == "manual_confirm" or body.get("manual") is True:
            manual_items += 1

    nodes = len(node_ids)
    if not events:
        # 无执行事件时从台账摘要回填（字段齐全且与引擎口径一致）
        s = {}
        try:
            s = json.loads(run.get("summary_json") or "{}")
        except (ValueError, TypeError):
            s = {}
        nodes = _as_int(s.get("nodes"))
        duration_ms = float(s.get("duration_ms") or 0)
        cache_hits = _as_int(s.get("cache_hits"))
        blocks = _as_int(s.get("blocks"))
        writebacks = _as_int(s.get("writebacks"))
        manual_items = _as_int(s.get("manual_items"))

    return {
        "run_id": run_id,
        "replay": True,
        "nodes": nodes,
        "duration_ms": round(duration_ms, 3),
        "cache_hits": cache_hits,
        "blocks": blocks,
        "writebacks": writebacks,
        "manual_items": manual_items,
        "status": run.get("status", "unknown"),
        "template_id": run.get("template_id", ""),
        "template_version": run.get("template_version", ""),
        "started_at": run.get("started_at", ""),
        "finished_at": run.get("finished_at", ""),
        "generated_at": now_iso(),
        "replay_note": "本摘要为只读回放，未触发任何写请求，不代表真实写回",
    }


def _redact(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in _PHI_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(x) for x in obj]
    return obj


def write_replay_summary(summary, path) -> Path:
    """把回放摘要落盘为脱敏 JSON 产物，返回落盘路径。"""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    clean = _redact(dict(summary))
    p.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return p
