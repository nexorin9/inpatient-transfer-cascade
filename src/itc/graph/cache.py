"""落盘缓存：键 = 节点签名 + 业务哈希（不含时间戳 / 重试次数）。

对应 ComfyUI comfy_execution/graph.py:206 is_cached() / :209 cache_link() /
:230 cache_update() 的内存缓存，本轮改为 SQLite 落盘，并加入失效传播与脏标记：
- 键纯由 (节点签名, 业务哈希) 决定，重启后新连接仍可命中；
- 每条缓存记录产出摘要 / 产出状态 / 写入时间 / 来源（取数 / 判定 / 写回）；
- 上游输入哈希变化沿连线递归标脏（写 cache_dirty），未受影响分支保持命中。

本模块同时提供图结构规范化辅助（G1 Graph 对象 / 多种 dict 形态 -> 统一 nodes/links），
供 engine.py 复用；不 import H/M/U 组模块。
"""
from __future__ import annotations

import json
import threading

from itc.store.db import now_iso


def cache_key(node_signature: str, business_hash: str) -> str:
    """缓存键：节点签名 + 业务哈希。纯函数，不含时间戳 / 随机数 / 重试次数。"""
    return f"{node_signature}::{business_hash}"


# ---------------------------------------------------------------------------
# 图结构规范化：把 G1 Graph 对象 / prompt 形态 dict / 显式 nodes-links dict
# 统一成 {id: info} 与 [(from_id, to_id)]，供缓存失效传播与执行主循环共用。
# ---------------------------------------------------------------------------

_META_KEYS = {
    "links", "template_id", "template_version", "meta", "version", "schema_version",
    "transfer_no", "inpatient_no", "_note",
}


def _graph_as_dict(graph) -> dict | None:
    if isinstance(graph, dict):
        return graph
    if graph is not None and hasattr(graph, "to_dict"):
        try:
            d = graph.to_dict()
            if isinstance(d, dict):
                return d
        except Exception:
            return None
    return None


def _nodes_from_dict(d: dict) -> dict:
    if isinstance(d.get("nodes"), dict):
        return {str(k): (v if isinstance(v, dict) else {"class_type": str(v)}) for k, v in d["nodes"].items()}
    if isinstance(d.get("nodes"), list):
        out = {}
        for i, n in enumerate(d["nodes"]):
            if isinstance(n, dict):
                nid = str(n.get("id") or n.get("node_id") or f"node_{i}")
                out[nid] = n
            else:
                out[f"node_{i}"] = {"class_type": str(n)}
        return out
    # prompt 形态：{node_id: {class_type, inputs, ...}}，跳过顶层元信息键
    return {
        str(k): (v if isinstance(v, dict) else {"class_type": str(v)})
        for k, v in d.items()
        if k not in _META_KEYS
    }


def _graph_nodes(graph) -> dict:
    d = _graph_as_dict(graph)
    if isinstance(d, dict):
        nodes = _nodes_from_dict(d)
        if nodes:
            return nodes
    # 兜底：仅有 node_ids() 的最简对象
    if graph is not None and hasattr(graph, "node_ids"):
        try:
            return {str(nid): {"class_type": ""} for nid in graph.node_ids()}
        except Exception:
            return {}
    return {}


def _is_link_value(v) -> bool:
    """连线输入常写作 [from_node_id, from_socket] / (from, socket)。"""
    return (
        isinstance(v, (list, tuple))
        and len(v) >= 2
        and isinstance(v[0], (str, int))
        and not isinstance(v[0], bool)
    )


def _link_pair(lk):
    if isinstance(lk, dict):
        frm = str(lk.get("from_node_id") or lk.get("from") or lk.get("source") or "")
        to = str(lk.get("to_node_id") or lk.get("to") or lk.get("target") or "")
        return (frm, to) if frm and to else None
    if isinstance(lk, (list, tuple)) and len(lk) >= 2:
        frm = str(lk[0])
        to = str(lk[2]) if len(lk) >= 3 else str(lk[1])
        return (frm, to)
    return None


def _graph_links(graph, nodes: dict) -> list[tuple[str, str]]:
    """统一成 (from_id, to_id) 列表：显式 links + prompt 形态 inputs 里的连线值。"""
    links: list[tuple[str, str]] = []
    d = _graph_as_dict(graph)
    if isinstance(d, dict) and isinstance(d.get("links"), (list, tuple)):
        for lk in d["links"]:
            pair = _link_pair(lk)
            if pair:
                links.append(pair)
    for nid, info in nodes.items():
        if not isinstance(info, dict):
            continue
        inputs = info.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for val in inputs.values():
            if _is_link_value(val):
                frm = str(val[0])
                if frm and frm != nid:
                    links.append((frm, nid))
            elif isinstance(val, dict):
                frm = str(val.get("from_node_id") or val.get("link") or val.get("from") or "")
                if frm and frm != nid:
                    links.append((frm, nid))
    seen = set()
    out = []
    for a, b in links:
        if (a, b) not in seen:
            seen.add((a, b))
            out.append((a, b))
    return out


def _downstream_map(graph, nodes: dict) -> dict:
    dm: dict[str, list[str]] = {}
    for a, b in _graph_links(graph, nodes):
        dm.setdefault(a, []).append(b)
    return dm


class CacheStore:
    """落盘缓存的读写入口与失效传播。"""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._lock = threading.RLock()
        self._hits = 0
        self._last_affected: set[str] = set()

    # 允许 CacheStore.cache_key(...) 与模块级 cache_key(...) 两种用法
    cache_key = staticmethod(cache_key)

    def get(self, key):
        row = self.conn.execute(
            "SELECT cache_key,node_id,run_id,input_hash,output_summary,output_json,status,source,created_at "
            "FROM cache_entries WHERE cache_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["output"] = json.loads(d.get("output_json") or "null")
        except (ValueError, TypeError):
            d["output"] = None
        return d

    def set(self, key, *, node_id, input_hash, output, summary="", status="ok", source="", run_id=""):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache_entries"
                "(cache_key,node_id,run_id,input_hash,output_summary,output_json,status,source,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    node_id,
                    run_id,
                    input_hash,
                    summary,
                    json.dumps(output, ensure_ascii=False, sort_keys=True, default=str),
                    status,
                    source,
                    now_iso(),
                ),
            )
            self.conn.commit()
        return key

    def _is_dirty(self, node_id) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM cache_dirty WHERE node_id=? LIMIT 1", (node_id,)
        ).fetchone()
        return row is not None

    def is_cached(self, key, input_hash) -> bool:
        """命中判定：键存在且输入哈希一致且节点未被标脏 -> 命中。"""
        entry = self.get(key)
        if entry is None:
            return False
        if entry["input_hash"] != input_hash:
            return False
        if self._is_dirty(entry["node_id"]):
            return False
        self._hits += 1
        return True

    def _blocked_nodes(self, run_id="") -> set[str]:
        if run_id:
            rows = self.conn.execute(
                "SELECT node_id FROM external_blocks WHERE status='pending' AND run_id=?", (run_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT node_id FROM external_blocks WHERE status='pending'"
            ).fetchall()
        return {r["node_id"] for r in rows}

    def _mark_dirty(self, node_id, run_id) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache_dirty(node_id,run_id,marked_at) VALUES (?,?,?)",
                (node_id, run_id, now_iso()),
            )
            self.conn.commit()

    def clear_dirty(self, node_id) -> None:
        """节点重算成功后清除脏标记，使其重新参与缓存命中。"""
        with self._lock:
            self.conn.execute("DELETE FROM cache_dirty WHERE node_id=?", (node_id,))
            self.conn.commit()

    def invalidate(self, node_id, graph=None, run_id="") -> set[str]:
        """沿连线递归标脏，返回本次变更影响的节点集合。

        传播边界：下游节点不存在于图中，或该节点已有 pending 外部阻塞时，
        该分支终止（不标脏、不继续向下传播）；起点节点总是被标记。
        """
        nodes = _graph_nodes(graph)
        dm = _downstream_map(graph, nodes)
        # 传播边界只认本次 run 的 pending 阻塞；未指定 run_id 时退化为全局查询
        blocked = self._blocked_nodes(run_id)
        affected: set[str] = set()
        stack = [node_id]
        while stack:
            cur = stack.pop()
            if cur in affected:
                continue
            if cur != node_id:
                if cur not in nodes or cur in blocked:
                    continue
            affected.add(cur)
            self._mark_dirty(cur, run_id)
            for dn in dm.get(cur, []):
                if dn not in affected:
                    stack.append(dn)
        self._last_affected = set(affected)
        return set(affected)

    def affected_set(self, run_id=None) -> set[str]:
        if run_id:
            rows = self.conn.execute(
                "SELECT DISTINCT node_id FROM cache_dirty WHERE run_id=?", (run_id,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT DISTINCT node_id FROM cache_dirty").fetchall()
        return {r["node_id"] for r in rows}

    def summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT status,source,COUNT(*) AS n FROM cache_entries GROUP BY status,source"
        ).fetchall()
        by_source: dict[str, int] = {}
        by_status: dict[str, int] = {}
        total = 0
        for r in rows:
            n = int(r["n"])
            total += n
            s = r["source"] or ""
            t = r["status"] or ""
            by_source[s] = by_source.get(s, 0) + n
            by_status[t] = by_status.get(t, 0) + n
        dirty = int(self.conn.execute("SELECT COUNT(*) AS n FROM cache_dirty").fetchone()["n"])
        return {
            "entries": total,
            "by_source": by_source,
            "by_status": by_status,
            "dirty": dirty,
            "hits": self._hits,
        }
