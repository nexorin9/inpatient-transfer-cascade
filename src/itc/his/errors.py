"""HIS 出站错误分类、指数退避重试与待重写队列。

- 4xx：字段错 / 权限不足，不可重试，直接失败并给出可读原因。
- 5xx：服务端临时故障，可重试（<= 2 次，指数退避）。
- 网络异常：连接失败 / 超时，按可重试处理。
- 超次仍失败：登记待重写队列（pending_writebacks 表），不静默丢弃。

本模块只依赖底座（store/db 的时间函数），不 import 其它业务组模块。
"""
from __future__ import annotations

import time

import httpx

from itc.store.db import now_iso


class HisError(RuntimeError):
    """HIS 出站错误基类。"""


class RetryableHisError(HisError):
    """可重试的 HIS 出站错误（5xx）。"""

    def __init__(self, message="", *, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class NonRetryableHisError(HisError):
    """不可重试的 HIS 出站错误（4xx / 越权 / 非法路径）。"""

    def __init__(self, message="", *, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class NetworkHisError(RetryableHisError):
    """网络异常（连接失败 / 超时），按可重试处理。"""


def classify_status(code) -> str:
    """把 HTTP 状态码归约为 retryable / non_retryable 两类。

    契约只约定 4xx 与 5xx：5xx 可重试；其余（含 4xx 与非预期码）保守归为
    不可重试，避免对客户端错误反复重试。返回值恒为这两个字符串之一。
    """
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "non_retryable"
    if 500 <= code < 600:
        return "retryable"
    return "non_retryable"


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试（指数退避据此决定是否继续）。"""
    if isinstance(exc, NonRetryableHisError):
        return False
    if isinstance(exc, (RetryableHisError, NetworkHisError)):
        return True
    if isinstance(exc, httpx.RequestError):
        return True
    return False


def retry_with_backoff(fn, *, max_retries=2, base_delay=0.0, on_retry=None):
    """指数退避重试：仅对可重试异常重试，最多 max_retries 次。

    总调用次数 = 1 + 实际重试次数（<= 1 + max_retries）。base_delay 为第 1 次
    重试前的基准间隔，之后按 2 的幂增长（attempt=1 -> base_delay，attempt=2 ->
    base_delay*2）。on_retry(attempt, exc) 在每次重试前回调，供写审计事件。
    超次或不可重试时抛出原始异常，绝不吞掉、绝不静默降级。
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # 需按类型决定是否重试，故捕获 Exception
            if not is_retryable(exc) or attempt >= max_retries:
                raise
            attempt += 1
            if on_retry is not None:
                on_retry(attempt, exc)
            delay = base_delay * (2 ** (attempt - 1))
            if delay > 0:
                time.sleep(delay)


def queue_pending_writeback(
    conn,
    *,
    disposition_id=None,
    idempotency_key="",
    attempts=0,
    reason="",
) -> int:
    """把超次重试的写回登记进 pending_writebacks 表（只增不改），返回主键。

    入队后等待托盘提示与人工重试；重试仍走同一幂等键。不静默丢弃该次写回。
    """
    cur = conn.execute(
        "INSERT INTO pending_writebacks"
        "(disposition_id, idempotency_key, attempts, reason, created_at)"
        " VALUES (?,?,?,?,?)",
        (disposition_id, idempotency_key, int(attempts), reason, now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)
