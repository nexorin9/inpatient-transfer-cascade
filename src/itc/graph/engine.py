"""执行主循环 + 工作线程 + 入队前校验 + 节点级进度事件。

对应：
- ComfyUI main.py:351 prompt_worker(q, server_instance)：进程内队列 + 后台工作线程；
- ComfyUI server.py:1072 POST /prompt -> validate_prompt：入队前校验；
- ComfyUI main.py:399 q.task_done(..., status=ExecutionStatus(...))：成功/失败/阻断终态；
- ComfyUI server.py:1469 send_progress_text(text, node_id, sid)：按节点顺序发进度事件；
- app/node_replace_manager.py:35 register()：同 key 重复注册被忽略。

本轮为桌面进程内队列，不引入 HTTP 服务面。G1 的 idempotency_key / scheduler
按契约 §2 G1 的 API 调用（文件暂未就位时用等价兜底，不自行创建 G1 文件）。
不 import H/M/U 组模块。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field

from itc.store.db import now_iso

from itc.graph.events import EventBus, NODE_STATUS, ProgressEvent
from itc.graph.audit import (
    emit_node_event,
    EVENT_NODE_START,
    EVENT_NODE_END,
    EVENT_NODE_BLOCKED,
    EVENT_NODE_RETRY,
    EVENT_WRITEBACK,
    EVENT_CACHE_HIT,
    EVENT_NODE_FAILED,
    EVENT_MANUAL_CONFIRM,
)
from itc.graph.cache import (
    CacheStore,
    cache_key,
    _graph_nodes,
    _graph_links,
)
from itc.graph.external_block import ExternalBlockStore
from itc.graph import template as _template

# --- G1 幂等键：按契约调用；G1 文件暂不在时提供等价 sha256 兜底 ---
try:
    from itc.graph.idempotency import idempotency_key  # type: ignore  # G1
except Exception:  # pragma: no cover - G1 尚未落盘时的等价兜底
    def idempotency_key(transfer_no, order_no, execute_dept_code, effective_from):
        raw = "|".join(
            str(x) for x in (transfer_no, order_no, execute_dept_code, effective_from)
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_WRITEBACK_TYPES = {
    "change_site", "stop_draft", "writeback", "write_execution_site",
}
_TERMINAL = {"success", "failed", "blocked", "cancelled"}


@dataclass
class RunResult:
    run_id: str
    status: str
    executed: list[str] = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "executed": list(self.executed),
            "errors": list(self.errors),
        }


def _input_hash(node_id, info, transfer_no) -> str:
    """节点输入业务哈希：只由节点定义与转科单号决定，不含时间戳 / 重试次数 / run_id。"""
    payload = {
        "node_id": node_id,
        "class_type": str(info.get("class_type") or ""),
        "inputs": info.get("inputs") or {},
        "transfer_no": transfer_no,
    }
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _is_link(v) -> bool:
    return (
        isinstance(v, (list, tuple))
        and len(v) >= 2
        and isinstance(v[0], (str, int))
        and not isinstance(v[0], bool)
    )


def _local_cycle(nodes: dict, links: list) -> list[str]:
    indeg = {n: 0 for n in nodes}
    adj = {n: [] for n in nodes}
    for a, b in links:
        if a in adj and b in indeg:
            adj[a].append(b)
            indeg[b] += 1
    ready = [n for n in nodes if indeg[n] == 0]
    seen = 0
    while ready:
        n = ready.pop(0)
        seen += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    if seen == len(nodes):
        return []
    return [n for n in nodes if indeg[n] > 0]


def _local_topo(nodes: dict, links: list) -> list[str]:
    indeg = {n: 0 for n in nodes}
    adj = {n: [] for n in nodes}
    for a, b in links:
        if a in adj and b in indeg:
            adj[a].append(b)
            indeg[b] += 1
    ready = sorted(n for n in nodes if indeg[n] == 0)
    out = []
    while ready:
        n = ready.pop(0)
        out.append(n)
        for m in sorted(adj[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
        ready.sort()
    if len(out) != len(nodes):
        out += [n for n in nodes if n not in out]
    return out


class Engine:
    """图执行引擎：submit 入队 / run 主循环 / start_worker 后台执行 / status / cancel。"""

    def __init__(self, conn, bus=None, executor=None, template_version=None) -> None:
        self.conn = conn
        self.bus = bus or EventBus()
        self.executor = executor
        self.template_version = template_version
        self.cache = CacheStore(conn)
        self.blocks = ExternalBlockStore(conn)
        self._lock = threading.RLock()
        self._registry: dict[str, dict] = {}
        self._rejections: dict[str, str] = {}
        self.last_rejection = ""
        self._cancelled: set[str] = set()
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._results: dict[str, RunResult] = {}
        self._audit_errors: list[str] = []

    # ------------------------------------------------------------------
    # 归一化 / 校验 / 注册
    # ------------------------------------------------------------------
    def _normalize(self, graph) -> dict:
        nodes = _graph_nodes(graph)
        links = _graph_links(graph, nodes)
        d = {"nodes": nodes, "links": links}
        raw = None
        if isinstance(graph, dict):
            raw = graph
        elif hasattr(graph, "to_dict"):
            try:
                raw = graph.to_dict()
            except Exception:
                raw = None
        if isinstance(raw, dict):
            d["template_id"] = raw.get("template_id")
            d["template_version"] = raw.get("template_version") or raw.get("version")
        return d

    def _resolved_template(self) -> tuple[str, str]:
        v = self.template_version if self.template_version is not None else _template.TEMPLATE_VERSION
        return _template.template_source(
            {"template_id": _template.DEFAULT_TEMPLATE_ID, "template_version": v}
        )

    def _detect_cycle(self, graph, nodes, links) -> list[str]:
        local = _local_cycle(nodes, links)
        if local:
            return local
        # 按契约调用 G1 scheduler.detect_cycle，作为补充信号（结果需落在当前节点集内）
        if graph is not None and not isinstance(graph, dict):
            try:
                from itc.graph.scheduler import detect_cycle  # G1
                c = [str(x) for x in (detect_cycle(graph) or [])]
                if c and all(x in nodes for x in c):
                    return c
            except Exception:
                pass
        return []

    def _order_nodes(self, graph, nodes, links) -> list[str]:
        # 按契约调用 G1 scheduler.ready_order，结果需是节点集的一个排列才采信
        if graph is not None and not isinstance(graph, dict):
            try:
                from itc.graph.scheduler import ready_order  # G1
                order = [str(x) for x in (ready_order(graph) or [])]
                if order and set(order) == set(nodes):
                    return order
            except Exception:
                pass
        return _local_topo(nodes, links)

    def _validate(self, graph, gd) -> tuple[bool, str]:
        nodes = gd["nodes"]
        if not nodes:
            return False, "图结构非法：图中没有任何节点"
        for nid, info in nodes.items():
            if not isinstance(info, dict):
                return False, f"缺必填输入：节点 {nid} 缺少节点定义"
            inputs = info.get("inputs") or {}
            for key in (info.get("required_inputs") or []):
                if key not in inputs:
                    return False, f"缺必填输入：节点 {nid} 缺少必填输入 {key}"
            if isinstance(inputs, dict):
                for key, val in inputs.items():
                    if _is_link(val) and str(val[0]) not in nodes:
                        return False, (
                            f"缺必填输入：节点 {nid} 的输入 {key} 引用了不存在的上游节点 {val[0]}"
                        )
        for a, b in gd["links"]:
            if a not in nodes or b not in nodes:
                return False, f"图结构非法：连线端点不存在（{a} -> {b}）"
        cycle = self._detect_cycle(graph, nodes, gd["links"])
        if cycle:
            return False, "图结构非法：存在依赖环（" + " -> ".join(cycle) + "）"
        tver = gd.get("template_version") or self.template_version
        if tver is not None and not _template.known_version(tver):
            return False, (
                f"模板版本不识别：{tver!r}（当前支持 {_template.TEMPLATE_VERSION} "
                f"及可迁移历史版本 {', '.join(_template.LEGACY_VERSIONS)}）"
            )
        return True, ""

    def _run_exists(self, run_id) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM graph_runs WHERE run_id=?", (run_id,)
        ).fetchone() is not None

    def _prepare(self, run_id, graph, transfer_no):
        """校验 + 注册（不含入队）。返回归一化图 dict；失败返回 None。"""
        gd = self._normalize(graph)
        ok, reason = self._validate(graph, gd)
        if not ok:
            self._rejections[run_id] = reason
            self.last_rejection = reason
            return None
        with self._lock:
            # 同 key 重复注册忽略：内存登记 + graph_runs 落库双重判定
            if run_id in self._registry or self._run_exists(run_id):
                reason = "同 key 重复注册：run_id 已存在，忽略"
                self._rejections[run_id] = reason
                self.last_rejection = reason
                return None
            tid, tver = self._resolved_template()
            tid = str(gd.get("template_id") or tid)
            tver = str(gd.get("template_version") or tver)
            # 迁移到当前 schema 的版本一并留痕（幂等）
            migrated = _template.migrate_template(
                {"template_id": tid, "template_version": tver}
            )
            self._registry[run_id] = {
                "graph": graph,
                "graph_dict": gd,
                "transfer_no": transfer_no,
                "template_id": tid,
                "template_version": tver,
                "migrated_version": migrated["template_version"],
                "affected": None,
            }
            self.conn.execute(
                "INSERT INTO graph_runs"
                "(run_id,transfer_no,template_id,template_version,status,started_at,finished_at,summary_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (run_id, transfer_no, tid, tver, "pending", now_iso(), None, None),
            )
            self.conn.commit()
        return gd

    def submit(self, run_id, graph, *, transfer_no="") -> bool:
        """入队前校验并注册；合法返回 True 并入队，拒收返回 False 且给出可读原因。"""
        gd = self._prepare(run_id, graph, transfer_no)
        if gd is None:
            return False
        self._queue.put(run_id)
        return True

    def rejection_reason(self, run_id) -> str:
        return self._rejections.get(run_id, "")

    # ------------------------------------------------------------------
    # 执行主循环
    # ------------------------------------------------------------------
    def _call_executor(self, run_id, node_id, info, transfer_no):
        fn = self.executor.get(node_id) if isinstance(self.executor, dict) else self.executor
        if fn is None:
            return {
                "status": "ok", "output": {}, "summary": f"{node_id} 默认执行",
                "source": str(info.get("source") or ""), "manual": False, "retries": 0,
            }
        args = (node_id, info, run_id, transfer_no)
        try:
            sig = inspect.signature(fn)
            n = 0
            varargs = False
            for p in sig.parameters.values():
                if p.kind == p.VAR_POSITIONAL:
                    varargs = True
                elif p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                    n += 1
            if not varargs and 0 < n < len(args):
                args = args[:n]
        except (ValueError, TypeError):
            pass
        return fn(*args)

    @staticmethod
    def _normalize_result(result) -> dict:
        if result is None:
            return {"status": "ok", "output": {}, "summary": "", "source": "",
                    "manual": False, "retries": 0, "actor": ""}
        if isinstance(result, dict):
            return {
                "status": str(result.get("status", "ok")),
                "output": result.get("output"),
                "summary": str(result.get("summary", "")),
                "source": str(result.get("source", "")),
                "manual": bool(result.get("manual", False)),
                "retries": int(result.get("retries", 0) or 0),
                "actor": str(result.get("actor", "")),
            }
        return {"status": "ok", "output": result, "summary": str(result),
                "source": "", "manual": False, "retries": 0, "actor": ""}

    def _emit_lifecycle(self, run_id, node_id, node_type, event_type, payload) -> None:
        p = dict(payload)
        p.setdefault("node_type", node_type)
        p.setdefault("node_id", node_id)
        try:
            emit_node_event(
                self.conn, run_id, node_id,
                event_type=event_type, node_type=node_type, payload=p,
                actor=str(p.get("actor", "")), table="execution_events",
            )
        except Exception as exc:  # 审计写入异常单独留痕，不静默吞掉
            self._audit_errors.append(f"{node_id}/{event_type}: {type(exc).__name__}: {exc}")

    def _wb_key(self, info, transfer_no) -> str:
        inputs = info.get("inputs") or {}
        order_no = str(inputs.get("order_no") or info.get("order_no") or "")
        dept = str(inputs.get("execute_dept_code") or info.get("execute_dept_code") or "")
        eff = str(inputs.get("effective_from") or info.get("effective_from") or "")
        return idempotency_key(transfer_no, order_no, dept, eff)

    def _wb_registered(self, key) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM writeback_registry WHERE idempotency_key=?", (key,)
        ).fetchone() is not None

    def _register_wb(self, key, run_id, node_id, info, transfer_no) -> None:
        inputs = info.get("inputs") or {}
        order_no = str(inputs.get("order_no") or info.get("order_no") or "")
        digest = hashlib.sha256(f"{key}|{node_id}".encode("utf-8")).hexdigest()[:16]
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO writeback_registry"
                "(idempotency_key,transfer_no,order_no,status_code,response_digest,disposition_id,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (key, transfer_no, order_no, 200, digest, None, now_iso()),
            )
            self.conn.commit()

    def _set_status(self, run_id, status, started_at=None) -> None:
        with self._lock:
            if started_at:
                self.conn.execute(
                    "UPDATE graph_runs SET status=?,started_at=? WHERE run_id=?",
                    (status, started_at, run_id),
                )
            else:
                self.conn.execute(
                    "UPDATE graph_runs SET status=? WHERE run_id=?", (status, run_id)
                )
            self.conn.commit()

    def _record_terminal(self, run_id, result, meta, started=None, finished=None, summary=None) -> None:
        started = started or now_iso()
        finished = finished or now_iso()
        payload = {
            "status": result.status,
            "executed": list(result.executed),
            "errors": list(result.errors),
            "template_id": (meta or {}).get("template_id", ""),
            "template_version": (meta or {}).get("template_version", ""),
            "migrated_version": (meta or {}).get("migrated_version", ""),
            **(summary or {}),
        }
        with self._lock:
            self.conn.execute(
                "UPDATE graph_runs SET status=?,started_at=?,finished_at=?,summary_json=? WHERE run_id=?",
                (
                    result.status, started, finished,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                    run_id,
                ),
            )
            self.conn.commit()

    def run(self, run_id) -> RunResult:
        """同步执行已注册的图；终态 success / failed / blocked 各落 graph_runs。

        失败不吞异常：异常的消息与 traceback 写入 RunResult.errors 与 graph_runs.summary_json，
        并产生 node_failed 审计事件。
        """
        with self._lock:
            meta = self._registry.get(run_id)
        if meta is None:
            result = RunResult(
                run_id=run_id, status="failed",
                errors=[{"node_id": "", "error": "运行未注册：找不到图定义"}],
            )
            self._record_terminal(run_id, result, meta)
            return result
        if run_id in self._cancelled:
            result = RunResult(run_id=run_id, status="cancelled")
            self._record_terminal(run_id, result, meta)
            return result

        graph = meta["graph"]
        gd = meta["graph_dict"]
        nodes = gd["nodes"]
        order = self._order_nodes(graph, nodes, gd["links"])
        transfer_no = meta["transfer_no"]
        affected = set(meta.get("affected") or ())
        blocked_ids = self.blocks.blocked_node_ids(run_id)

        executed: list[str] = []
        errors: list = []
        status = "success"
        started = now_iso()
        total_duration = 0.0
        summary = {
            "template_id": meta["template_id"],
            "template_version": meta["template_version"],
            "migrated_version": meta.get("migrated_version", ""),
            "nodes": 0, "duration_ms": 0.0, "cache_hits": 0, "blocks": 0,
            "writebacks": 0, "manual_items": 0,
        }
        self._set_status(run_id, "running", started_at=started)

        for node_id in order:
            if run_id in self._cancelled:
                status = "cancelled"
                break
            info = nodes.get(node_id) or {}
            class_type = str(info.get("class_type") or "")
            node_type = class_type or str(info.get("type") or "unknown")
            source = str(info.get("source") or "")

            # 阻断语义：遇到外部阻塞 / external_block 节点 -> 图停在这一步，下游不入队
            if node_id in blocked_ids or class_type == "external_block":
                if node_id not in blocked_ids:
                    self.blocks.create(
                        run_id, node_id,
                        {"reason": "需人工判定", "class_type": class_type},
                    )
                    blocked_ids.add(node_id)
                self._emit_lifecycle(
                    run_id, node_id, node_type, EVENT_NODE_BLOCKED,
                    {"summary": "外部阻塞，等待人工确认", "status": "blocked"},
                )
                self.bus.emit(ProgressEvent(
                    run_id, node_id, node_type, NODE_STATUS["blocked"], "外部阻塞等待人工确认"
                ))
                summary["blocks"] += 1
                status = "blocked"
                break

            input_hash = _input_hash(node_id, info, transfer_no)
            sig = f"{node_id}:{class_type}"
            key = cache_key(sig, input_hash)
            is_wb = source == "writeback" or class_type in _WRITEBACK_TYPES

            # 命中缓存且不在受影响集合 -> 跳过执行（不发起取数 / 写回请求）
            if node_id not in affected and self.cache.is_cached(key, input_hash):
                self._emit_lifecycle(
                    run_id, node_id, node_type, EVENT_CACHE_HIT,
                    {"summary": "命中落盘缓存", "cache_hit": True, "source": source},
                )
                self.bus.emit(ProgressEvent(
                    run_id, node_id, node_type, NODE_STATUS["cached"], "命中缓存"
                ))
                executed.append(node_id)
                summary["cache_hits"] += 1
                continue

            # 写回幂等：同一幂等键已登记 -> 复用，不产生第二次写请求
            wb_key = None
            if is_wb:
                wb_key = self._wb_key(info, transfer_no)
                if self._wb_registered(wb_key):
                    self._emit_lifecycle(
                        run_id, node_id, node_type, EVENT_WRITEBACK,
                        {"summary": "幂等命中，复用既有写回结果", "idempotency_key": wb_key,
                         "idempotent_hit": True, "source": "writeback"},
                    )
                    self.bus.emit(ProgressEvent(
                        run_id, node_id, node_type, NODE_STATUS["success"], "幂等命中"
                    ))
                    self.cache.clear_dirty(node_id)
                    executed.append(node_id)
                    summary["writebacks"] += 1
                    continue

            self._emit_lifecycle(
                run_id, node_id, node_type, EVENT_NODE_START, {"source": source}
            )
            t0 = time.perf_counter()
            try:
                raw = self._call_executor(run_id, node_id, info, transfer_no)
            except Exception as exc:
                duration = (time.perf_counter() - t0) * 1000.0
                err = {
                    "node_id": node_id, "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "duration_ms": round(duration, 3),
                }
                errors.append(err)
                self._emit_lifecycle(
                    run_id, node_id, node_type, EVENT_NODE_FAILED,
                    {"status": "failed", "error": str(exc), "duration_ms": round(duration, 3)},
                )
                self.bus.emit(ProgressEvent(
                    run_id, node_id, node_type, NODE_STATUS["failed"], str(exc)
                ))
                status = "failed"
                break
            duration = (time.perf_counter() - t0) * 1000.0
            total_duration += duration
            res = self._normalize_result(raw)

            # 执行器返回 blocked -> 登记阻塞并终止（下游不入队）
            if res["status"] == "blocked":
                self.blocks.create(
                    run_id, node_id,
                    {"reason": res.get("summary") or "需人工判定", "class_type": class_type},
                )
                self._emit_lifecycle(
                    run_id, node_id, node_type, EVENT_NODE_BLOCKED,
                    {"summary": res.get("summary", ""), "status": "blocked",
                     "duration_ms": round(duration, 3)},
                )
                self.bus.emit(ProgressEvent(
                    run_id, node_id, node_type, NODE_STATUS["blocked"], res.get("summary", "")
                ))
                summary["blocks"] += 1
                status = "blocked"
                break

            if res["status"] == "failed":
                err = {
                    "node_id": node_id,
                    "error": res.get("summary") or "节点返回失败",
                    "duration_ms": round(duration, 3),
                }
                errors.append(err)
                self._emit_lifecycle(
                    run_id, node_id, node_type, EVENT_NODE_FAILED,
                    {"status": "failed", "error": err["error"], "duration_ms": round(duration, 3)},
                )
                self.bus.emit(ProgressEvent(
                    run_id, node_id, node_type, NODE_STATUS["failed"], err["error"]
                ))
                status = "failed"
                break

            # 重试埋点
            for _ in range(res.get("retries", 0)):
                self._emit_lifecycle(
                    run_id, node_id, node_type, EVENT_NODE_RETRY, {"summary": "节点重试"}
                )

            # 写回埋点 + 幂等登记
            if is_wb or res.get("source") == "writeback":
                if wb_key is None:
                    wb_key = self._wb_key(info, transfer_no)
                self._register_wb(wb_key, run_id, node_id, info, transfer_no)
                self._emit_lifecycle(
                    run_id, node_id, node_type, EVENT_WRITEBACK,
                    {"summary": res.get("summary", ""), "idempotency_key": wb_key,
                     "source": "writeback", "duration_ms": round(duration, 3)},
                )
                summary["writebacks"] += 1

            # 人工确认埋点（记录操作人）
            if res.get("manual") or res.get("actor"):
                self._emit_lifecycle(
                    run_id, node_id, node_type, EVENT_MANUAL_CONFIRM,
                    {"summary": res.get("summary", ""), "actor": res.get("actor", "")},
                )

            # 节点结束事件 + 一条按节点顺序的进度事件
            self._emit_lifecycle(
                run_id, node_id, node_type, EVENT_NODE_END,
                {"status": res["status"], "summary": res.get("summary", ""),
                 "source": res.get("source") or source, "duration_ms": round(duration, 3),
                 "node_type": node_type},
            )
            self.bus.emit(ProgressEvent(
                run_id, node_id, node_type, NODE_STATUS["success"], res.get("summary", "")
            ))

            # 写落盘缓存：产出摘要 / 产出状态 / 写入时间 / 来源
            self.cache.set(
                key, node_id=node_id, input_hash=input_hash, output=res.get("output"),
                summary=res.get("summary", ""), status=res["status"],
                source=res.get("source") or source, run_id=run_id,
            )
            self.cache.clear_dirty(node_id)
            executed.append(node_id)
            if res.get("manual"):
                summary["manual_items"] += 1

        summary["nodes"] = len(executed)
        summary["duration_ms"] = round(total_duration, 3)
        finished = now_iso()
        result = RunResult(run_id=run_id, status=status, executed=executed, errors=errors)
        self._record_terminal(run_id, result, meta, started, finished, summary)
        with self._lock:
            self._results[run_id] = result
        return result

    # ------------------------------------------------------------------
    # 局部重跑
    # ------------------------------------------------------------------
    def partial_rerun(
        self, run_id, graph, *, transfer_no="", changed_node_id=None, affected=None
    ) -> RunResult:
        """以受影响集合重新入队：未受影响且命中缓存的节点跳过，结果追加台账而非覆盖。

        重跑写回仍走同一幂等键（writeback_registry 命中则不产生第二次写请求）。
        用新的 run_id 调用即为「追加」一条运行台账。
        """
        if affected is None and changed_node_id is not None:
            affected = self.cache.invalidate(changed_node_id, graph=graph, run_id=run_id)
        affected = set(affected or ())
        gd = self._prepare(run_id, graph, transfer_no)
        if gd is None:
            return RunResult(
                run_id=run_id, status="failed",
                errors=[{"node_id": "", "error": self.rejection_reason(run_id) or "局部重跑入队被拒"}],
            )
        with self._lock:
            self._registry[run_id]["affected"] = affected
        return self.run(run_id)

    # ------------------------------------------------------------------
    # 工作线程 / 状态 / 取消
    # ------------------------------------------------------------------
    def start_worker(self):
        """启动后台工作线程；入队与执行解耦，submit 不阻塞调用方。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker_loop, name="itc-engine-worker", daemon=True
            )
            self._thread.start()
            return self._thread

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self.run(run_id)
            except Exception:
                # 工作线程不因单次运行异常退出；异常写入终态台账可见（不吞异常）
                try:
                    self._record_terminal(
                        run_id,
                        RunResult(
                            run_id=run_id, status="failed",
                            errors=[{"node_id": "", "error": "工作线程执行异常",
                                     "traceback": traceback.format_exc()}],
                        ),
                        self._registry.get(run_id),
                    )
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    def stop_worker(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=5)
        self._thread = None

    def wait(self, run_id, timeout=5.0):
        """等待某 run 到达终态，返回最终状态字符串（供测试与调用方轮询）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self.status(run_id).get("status")
            if s in _TERMINAL:
                return s
            time.sleep(0.01)
        return self.status(run_id).get("status")

    def status(self, run_id) -> dict:
        row = self.conn.execute(
            "SELECT * FROM graph_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return {"run_id": run_id, "status": "unknown"}
        d = dict(row)
        try:
            d["summary"] = json.loads(d.get("summary_json") or "{}")
        except (ValueError, TypeError):
            d["summary"] = {}
        return d

    def cancel(self, run_id) -> bool:
        with self._lock:
            self._cancelled.add(run_id)
            row = self.conn.execute(
                "SELECT status FROM graph_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] not in _TERMINAL:
                self.conn.execute(
                    "UPDATE graph_runs SET status='cancelled' WHERE run_id=?", (run_id,)
                )
                self.conn.commit()
            return True
