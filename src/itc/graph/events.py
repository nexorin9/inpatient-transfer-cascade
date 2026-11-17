"""节点级进度事件与进程内事件总线。

对应 ComfyUI server.py:1469 send_progress_text(text, node_id, sid) 的进度回传语义：
本轮把「按节点回传」落成进程内事件对象，供桌面进度条 / 联动图消费。
本模块不接触 HIS 出站，也不涉及任何写请求。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from itc.store.db import now_iso

# 节点状态取值集合：进度事件与运行终态共用，取值均为 ASCII 字符串
NODE_STATUS = {
    "pending": "pending",
    "running": "running",
    "success": "success",
    "cached": "cached",
    "failed": "failed",
    "blocked": "blocked",
    "rejected": "rejected",
    "cancelled": "cancelled",
}


@dataclass
class ProgressEvent:
    """一条节点级进度事件：绑定 run_id 与 node_id，携带类型 / 状态 / 说明 / 时间。"""

    run_id: str
    node_id: str
    node_type: str = ""
    status: str = NODE_STATUS["pending"]
    message: str = ""
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = now_iso()

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "node_id": self.node_id,
            "node_type": self.node_type,
            "status": self.status,
            "message": self.message,
            "ts": self.ts,
        }

    # 与 dict 风格调用兼容的别名
    def as_dict(self) -> dict:
        return self.to_dict()


class EventBus:
    """线程安全的进程内事件总线：订阅回调 + 事件快照 + 取出并清空。"""

    def __init__(self) -> None:
        self._events: list[ProgressEvent] = []
        self._subscribers: list[Callable[[ProgressEvent], None]] = []
        self._lock = threading.RLock()
        # 订阅回调自身抛错时留痕，避免静默吞掉（但不阻断主循环）
        self.callback_errors: list[str] = []

    def subscribe(self, cb: Callable[[ProgressEvent], None]) -> None:
        with self._lock:
            if cb not in self._subscribers:
                self._subscribers.append(cb)

    def emit(self, event: ProgressEvent) -> None:
        with self._lock:
            self._events.append(event)
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(event)
            except Exception as exc:  # 回调异常单独记录，不阻断执行主循环
                with self._lock:
                    self.callback_errors.append(f"{type(exc).__name__}: {exc}")

    def events(self) -> list[ProgressEvent]:
        with self._lock:
            return list(self._events)

    def drain(self) -> list[ProgressEvent]:
        with self._lock:
            out = list(self._events)
            self._events.clear()
            return out

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
