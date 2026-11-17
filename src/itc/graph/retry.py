"""节点级重试、指数退避与熔断。

只依赖底座 store.db 的只增不改审计入口；不 import H/M/U 组模块。
可重试错误（网络 / 5xx / 超时）最多重试 max_retries 次；连续失败达
breaker_threshold 即熔断，熔断后直接失败、不再调用下游 HIS。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from itc.store.db import append_audit

# 终态节点状态集合：graceful_interrupt 等模块据此判断"未完成"
TERMINAL_NODE_STATES = frozenset(
    ("success", "done", "failed", "blocked", "skipped", "written_back")
)


@dataclass
class RetryPolicy:
    """重试策略。

    max_retries: 单次调用最多重试次数（总尝试次数 = max_retries + 1）。
    backoff_base: 退避基数，第 n 次重试前等待 backoff_base * 2**(n-1) 秒。
    breaker_threshold: 同一节点连续失败多少次触发熔断。
    """

    max_retries: int = 2
    backoff_base: float = 0.0
    breaker_threshold: int = 3

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1


class CircuitBreaker:
    """按节点统计连续失败次数，达阈值即打开（熔断）。"""

    def __init__(self, threshold: int = 3):
        self.threshold = int(threshold)
        self.failures = 0
        self._open = False
        self.last_reason = ""

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def is_closed(self) -> bool:
        return not self._open

    def record_failure(self, reason: str = "") -> bool:
        """记录一次失败；返回本次是否"刚触发熔断"。"""
        self.failures += 1
        self.last_reason = str(reason)
        was_open = self._open
        if self.failures >= self.threshold:
            self._open = True
        return self._open and not was_open

    def record_success(self) -> None:
        self.failures = 0
        self._open = False
        self.last_reason = ""

    def allow(self) -> bool:
        return not self._open

    def check(self) -> None:
        if self._open:
            raise NodeRetryError(
                "节点已熔断，不允许继续调用下游 HIS",
                reason="熔断已打开，停止向下游 HIS 发请求",
            )

    def reset(self) -> None:
        self.failures = 0
        self._open = False
        self.last_reason = ""

    def as_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "failures": self.failures,
            "is_open": self._open,
            "last_reason": self.last_reason,
        }


class NodeRetryError(RuntimeError):
    """节点重试耗尽或熔断后的失败：携带可读原因、重试次数与下游阻断清单。"""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        node_id: str = "",
        reason: str = "",
        downstream: Iterable[str] | None = None,
        last_error: BaseException | None = None,
        breaker_open: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.attempts = attempts
        self.node_id = node_id
        self.reason = reason or message
        self.downstream = list(downstream or [])
        self.last_error = last_error
        self.breaker_open = breaker_open

    def blocks(self, node_id: str) -> bool:
        """该失败是否阻断指定下游节点。"""
        return node_id in self.downstream

    def as_dict(self) -> dict:
        return {
            "error": "NodeRetryError",
            "message": self.message,
            "node_id": self.node_id,
            "reason": self.reason,
            "attempts": self.attempts,
            "downstream": list(self.downstream),
            "breaker_open": self.breaker_open,
        }


def default_is_retryable(exc: BaseException) -> bool:
    """默认错误分类：网络 / 5xx / 超时可重试，4xx、校验失败、取消不可重试。"""
    flag = getattr(exc, "retryable", None)
    if flag is True:
        return True
    if flag is False:
        return False

    name = type(exc).__name__.lower().replace("_", "").replace("-", "")
    if "nonretryable" in name or "nonretry" in name:
        return False

    code = None
    for attr in ("status_code", "http_status", "code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            code = value
            break
        if isinstance(value, str) and value.isdigit():
            code = int(value)
            break
    if code is not None:
        return 500 <= code < 600

    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, ConnectionError):
        return True
    if any(k in name for k in ("timeout", "timedout", "network", "connection", "connect")):
        return True
    # H 组 RetryableHisError 之类按名称兜底；NonRetryable 已在上面排除
    if "retryable" in name:
        return True
    if any(k in name for k in ("badgateway", "serviceunavailable", "servererror", "5xx")):
        return True
    return False


def _emit(
    conn: sqlite3.Connection | None,
    *,
    run_id: str,
    node_id: str,
    node_type: str,
    event_type: str,
    payload: dict,
) -> None:
    if conn is None:
        return
    append_audit(
        conn,
        run_id=run_id,
        node_id=node_id,
        node_type=node_type,
        event_type=event_type,
        actor="run_with_retry",
        payload=payload,
    )


def _emit_failure(
    conn: sqlite3.Connection | None,
    *,
    run_id: str,
    node_id: str,
    node_type: str,
    reason: str,
    attempts: int,
    downstream: list[str],
    breaker_open: bool,
) -> None:
    _emit(
        conn,
        run_id=run_id,
        node_id=node_id,
        node_type=node_type,
        event_type="node_failed",
        payload={
            "node_id": node_id,
            "reason": reason,
            "attempts": attempts,
            "breaker_open": breaker_open,
            "downstream": list(downstream),
        },
    )
    for dep in downstream:
        _emit(
            conn,
            run_id=run_id,
            node_id=dep,
            node_type=node_type,
            event_type="node_blocked",
            payload={
                "node_id": dep,
                "blocked_by": node_id,
                "reason": f"上游节点 {node_id} 失败/熔断：{reason}",
            },
        )


def _notify(on_retry: Callable | None, attempts: int, error: BaseException, delay: float) -> None:
    if on_retry is None:
        return
    # 兼容 3 参 / 2 参 / 1 参回调，避免 caller 签名不一致时吞掉重试
    for args in ((attempts, error, delay), (attempts, error), (attempts,)):
        try:
            on_retry(*args)
            return
        except TypeError:
            continue
    raise TypeError("on_retry 回调无法以 (attempt, error, delay) 调用")


def run_with_retry(
    fn: Callable[[], object],
    policy: RetryPolicy,
    on_retry: Callable | None = None,
    is_retryable: Callable[[BaseException], bool] | None = None,
    breaker: CircuitBreaker | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    run_id: str = "",
    node_id: str = "",
    node_type: str = "",
    downstream: Iterable[str] | None = None,
):
    """执行 fn；仅对可重试错误退避重试，熔断后直接失败并阻断下游。

    额外可选关键字参数 conn/run_id/node_id/node_type/downstream 用于把
    每次重试、熔断、失败、下游阻断写入只增不改的审计链；不传则纯内存执行。
    """
    classify = is_retryable or default_is_retryable
    deps = list(downstream or [])
    attempts = 0

    while True:
        if breaker is not None and breaker.is_open:
            reason = "节点已熔断，停止向下游 HIS 发请求"
            error = NodeRetryError(
                f"节点 {node_id} 已熔断，不再调用下游 HIS",
                attempts=attempts,
                node_id=node_id,
                reason=reason,
                downstream=deps,
                breaker_open=True,
            )
            _emit_failure(
                conn, run_id=run_id, node_id=node_id, node_type=node_type,
                reason=reason, attempts=attempts, downstream=deps, breaker_open=True,
            )
            raise error

        attempts += 1
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:
            duration_ms = round((time.monotonic() - started) * 1000, 3)

            if not classify(exc):
                # 不可重试错误：不重试，写失败审计后原样抛出，保留错误类型
                _emit(
                    conn, run_id=run_id, node_id=node_id, node_type=node_type,
                    event_type="node_failed",
                    payload={
                        "node_id": node_id,
                        "reason": f"不可重试错误：{exc}",
                        "attempts": attempts,
                        "duration_ms": duration_ms,
                        "breaker_open": False,
                    },
                )
                raise

            just_opened = False
            if breaker is not None:
                just_opened = breaker.record_failure(reason=str(exc))
            if just_opened:
                _emit(
                    conn, run_id=run_id, node_id=node_id, node_type=node_type,
                    event_type="node_circuit_open",
                    payload={
                        "node_id": node_id,
                        "failures": breaker.failures,
                        "threshold": breaker.threshold,
                        "reason": f"连续失败达阈值 {breaker.threshold}，熔断",
                    },
                )

            if attempts > policy.max_retries:
                reason = f"可重试错误连续失败，重试 {policy.max_retries} 次后仍失败：{exc}"
                error = NodeRetryError(
                    f"节点 {node_id} 失败：{reason}",
                    attempts=policy.max_retries,
                    node_id=node_id,
                    reason=reason,
                    downstream=deps,
                    last_error=exc,
                    breaker_open=breaker.is_open if breaker is not None else False,
                )
                _emit_failure(
                    conn, run_id=run_id, node_id=node_id, node_type=node_type,
                    reason=reason, attempts=attempts - 1, downstream=deps,
                    breaker_open=breaker.is_open if breaker is not None else False,
                )
                raise error from exc

            delay = policy.backoff_base * (2 ** (attempts - 1))
            _emit(
                conn, run_id=run_id, node_id=node_id, node_type=node_type,
                event_type="node_retry",
                payload={
                    "node_id": node_id,
                    "attempt": attempts,
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                    "duration_ms": duration_ms,
                    "backoff_delay": delay,
                },
            )
            _notify(on_retry, attempts, exc, delay)
            if delay > 0:
                time.sleep(delay)
            continue
        else:
            if breaker is not None:
                breaker.record_success()
            return result
