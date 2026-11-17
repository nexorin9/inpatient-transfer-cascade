"""节点级超时、全局取消与优雅中断。

只依赖底座 store.db（append_audit / now_iso / 表结构）与标准库。
取消是协作式的：调度器在发新请求前检查 CancellationToken，已发出的在途节点
先收尾；中断时把未完成节点标为可恢复并落盘，进程重启可续跑。
"""
from __future__ import annotations

import json
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterable

from itc.store.db import append_audit, now_iso

# 已完成/已终止的节点状态；其余状态均视为"未完成、可恢复"
TERMINAL_STATUSES = frozenset(
    ("success", "done", "failed", "blocked", "skipped", "written_back")
)


class CancelledError(RuntimeError):
    """收到取消信号后由 CancellationToken.check() 抛出的终止异常。"""

    def __init__(self, reason: str = ""):
        self.reason = reason or "执行已取消"
        super().__init__(self.reason)


class NodeTimeoutError(TimeoutError):
    """节点级超时。继承内置 TimeoutError，按可重试错误处理。"""


class CancellationToken:
    """跨线程取消信号：cancel(reason) 置位，is_cancelled / reason 可查询。"""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    def cancel(self, reason: str = "") -> None:
        with self._lock:
            if not self._event.is_set():
                self._reason = reason or "用户取消"
                self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def check(self) -> None:
        if self.is_cancelled:
            raise CancelledError(self.reason)


@contextmanager
def node_timeout(seconds: float):
    """节点级超时上下文：块内执行超过 seconds 抛 NodeTimeoutError（可重试）。

    主线程用 SIGALRM 抢占；非主线程无信号能力时退化为块结束后的截止校验。
    """
    if seconds <= 0:
        raise ValueError("节点超时秒数必须为正数")

    main_thread = threading.current_thread() is threading.main_thread()
    use_signal = main_thread and hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")

    if use_signal:
        def _handler(signum, frame):
            raise NodeTimeoutError(f"节点执行超时（>{seconds}s）")

        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        signal.signal(signal.SIGALRM, _handler)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            delay, interval = previous_timer
            if delay > 0:
                signal.setitimer(signal.ITIMER_REAL, delay, interval)
    else:
        started = time.monotonic()
        yield
        if time.monotonic() - started > seconds:
            raise NodeTimeoutError(f"节点执行超时（>{seconds}s）")


class GracefulRunner:
    """协作式调度器：收到取消后停止调度新节点、等待在途节点收尾、不再发新请求。

    dispatch(node_id) 代表一次向下游 HIS 发出的请求；返回值记录已发请求、
    已完成（收尾）节点与仍在途节点。
    """

    def __init__(self, token: CancellationToken, dispatch: Callable[[str], None]):
        self.token = token
        self.dispatch = dispatch
        self.requests: list[str] = []
        self.completed: list[str] = []
        self.in_flight: list[str] = []

    def run(self, node_ids: Iterable[str]) -> dict:
        for node_id in node_ids:
            if self.token.is_cancelled:
                break
            self.requests.append(node_id)
            self.in_flight.append(node_id)
            try:
                self.dispatch(node_id)
            finally:
                if node_id in self.in_flight:
                    self.in_flight.remove(node_id)
                self.completed.append(node_id)
        return {
            "requests": list(self.requests),
            "completed": list(self.completed),
            "in_flight": list(self.in_flight),
            "cancelled": self.token.is_cancelled,
        }


def _states_from_events(conn: sqlite3.Connection, run_id: str) -> dict[str, str]:
    """从 execution_events 推导节点状态：started 且无终态事件视为 running。"""
    rows = conn.execute(
        "SELECT node_id, event_type FROM execution_events WHERE run_id = ? ORDER BY seq ASC",
        (run_id,),
    ).fetchall()
    terminal_map = {
        "node_success": "success",
        "node_failed": "failed",
        "node_blocked": "blocked",
        "node_skipped": "skipped",
        "node_done": "done",
    }
    started: list[str] = []
    terminal: dict[str, str] = {}
    for row in rows:
        event_type = row["event_type"]
        node_id = row["node_id"]
        if event_type == "node_started":
            if node_id not in started:
                started.append(node_id)
        elif event_type in terminal_map:
            terminal[node_id] = terminal_map[event_type]
    return {node_id: terminal.get(node_id, "running") for node_id in started}


def _persist_interrupt(conn: sqlite3.Connection, run_id: str, summary: dict) -> None:
    """把中断摘要写入 graph_runs.summary_json（落盘、可查询、进程重启可读）。"""
    row = conn.execute(
        "SELECT summary_json FROM graph_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    existing: dict = {}
    if row is not None and row["summary_json"]:
        try:
            parsed = json.loads(row["summary_json"])
            if isinstance(parsed, dict):
                existing = parsed
        except (ValueError, TypeError):
            existing = {}
    existing["interrupt"] = summary
    payload = json.dumps(existing, ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO graph_runs(run_id, transfer_no, status, started_at, summary_json)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(run_id) DO UPDATE SET summary_json = excluded.summary_json",
        (run_id, str(existing.get("transfer_no", "")), "interrupted", now_iso(), payload),
    )
    conn.commit()


def _audit_interrupt(
    conn: sqlite3.Connection,
    run_id: str,
    token: CancellationToken,
    unfinished: list[dict],
    reason: str,
) -> None:
    append_audit(
        conn,
        run_id=run_id,
        node_id="__run__",
        node_type="graph",
        event_type="run_interrupted",
        actor="graceful_interrupt",
        payload={
            "reason": reason,
            "cancelled": token.is_cancelled,
            "unfinished_nodes": [u["node_id"] for u in unfinished],
            "unfinished_count": len(unfinished),
        },
    )
    for item in unfinished:
        append_audit(
            conn,
            run_id=run_id,
            node_id=item["node_id"],
            node_type="",
            event_type="node_recoverable",
            actor="graceful_interrupt",
            payload={
                "node_id": item["node_id"],
                "state": "recoverable",
                "recoverable": True,
                "reason": item["reason"],
            },
        )


def graceful_interrupt(
    conn: sqlite3.Connection,
    run_id: str,
    token: CancellationToken,
    *,
    node_states: dict[str, str] | None = None,
) -> dict:
    """优雅中断：把未完成节点标为可恢复并落盘，返回含清单与原因的中断摘要。

    node_states 为 {node_id: status}；不传时从 execution_events 推导。
    非终态节点均视为未完成，逐条记录可恢复状态与可读原因。
    """
    reason = token.reason if token.is_cancelled else "执行中断（未收到取消信号）"
    states = node_states if node_states is not None else _states_from_events(conn, run_id)

    unfinished: list[dict] = []
    for node_id, status in states.items():
        if str(status).lower() in TERMINAL_STATUSES:
            continue
        unfinished.append({
            "node_id": node_id,
            "previous_state": str(status),
            "state": "recoverable",
            "recoverable": True,
            "reason": f"{reason}：节点 {node_id} 未完成（状态 {status}），标记为可恢复",
        })

    summary = {
        "run_id": run_id,
        "cancelled": bool(token.is_cancelled),
        "reason": reason,
        "unfinished_nodes": unfinished,
        "unfinished_count": len(unfinished),
        "recoverable": True,
    }
    _persist_interrupt(conn, run_id, summary)
    _audit_interrupt(conn, run_id, token, unfinished, reason)
    return summary


def load_resume_state(conn: sqlite3.Connection, run_id: str) -> dict | None:
    """读取落盘的中断摘要，供进程重启后续跑；无记录返回 None。"""
    row = conn.execute(
        "SELECT summary_json FROM graph_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None or not row["summary_json"]:
        return None
    try:
        data = json.loads(row["summary_json"])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "interrupt" not in data:
        return None
    return data["interrupt"]


def recoverable_nodes(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """返回可从中断处续跑的未完成节点清单。"""
    state = load_resume_state(conn, run_id)
    if state is None:
        return []
    return list(state.get("unfinished_nodes", []))
