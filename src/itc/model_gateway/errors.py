"""模型调用错误分类、重试策略与「待人工判定」映射。

五类可区分错误：超时 / 连接失败 / HTTP 4xx·5xx（ModelHTTPError 带 status_code
区分）/ 响应不可解析 / schema 校验失败。契约只把「连接失败与 5xx」列为可重试，
4xx 与校验失败不重试；超时、4xx、不可解析、校验失败一律快速降级，不多发请求。
任何失败一律产出 UNDETERMINED + manual=True，绝不落到 KEEP / CHANGE_SITE。
"""
from __future__ import annotations

from itc.his.redact import redact_log


class ModelError(RuntimeError):
    """模型调用错误基类，携带耗时与重试次数，供日志与审计使用。"""

    error_type = "model_error"

    def __init__(self, message="", *, status_code=None, latency_ms=0.0, retries=0):
        super().__init__(message)
        self.status_code = status_code
        self.latency_ms = float(latency_ms)
        self.retries = int(retries)


class ModelTimeoutError(ModelError):
    error_type = "timeout"


class ModelConnectionError(ModelError):
    error_type = "connection"


class ModelHTTPError(ModelError):
    """HTTP 4xx / 5xx；error_type 依 status_code 为 http_4xx / http_5xx。"""

    def __init__(self, message="", *, status_code, latency_ms=0.0, retries=0):
        super().__init__(
            message, status_code=status_code, latency_ms=latency_ms, retries=retries
        )

    @property
    def error_type(self):
        code = self.status_code or 0
        return f"http_{code // 100}xx"


class ModelUnparsableError(ModelError):
    error_type = "unparsable"


class ModelSchemaError(ModelError):
    error_type = "schema"


def is_retryable(exc) -> bool:
    """连接失败与 5xx 可重试（<= 2 次）；4xx、超时、不可解析、校验失败不重试。"""
    if isinstance(exc, ModelConnectionError):
        return True
    if isinstance(exc, ModelHTTPError):
        return 500 <= (exc.status_code or 0) < 600
    return False


def error_log(exc, *, latency_ms=0.0, retries=0) -> dict:
    """错误日志字段：类型 / 耗时 / 重试次数 / 错误摘要。

    摘要只取异常消息（错误类型与状态码描述），不含住院号原文与医嘱全文；再经
    redact_log 兜底脱敏，保证日志落盘无 PHI。
    """
    record = {
        "error_type": getattr(exc, "error_type", "model_error"),
        "latency_ms": float(latency_ms),
        "retries": int(retries),
        "summary": str(exc),
    }
    return redact_log(record)


def failed_judgement(order_no, exc, *, latency_ms=0.0, retries=0) -> dict:
    """任何失败一律「待人工判定」：judgement=UNDETERMINED 且 manual=True。

    返回结构同时满足 JUDGEMENT_SCHEMA 的核心字段（额外字段默认放行），便于
    业务层用同一套结构处理成功与失败结果。
    """
    log = error_log(exc, latency_ms=latency_ms, retries=retries)
    return {
        "order_no": str(order_no),
        "judgement": "UNDETERMINED",
        "confidence": 0.0,
        "evidence_fields": [],
        "evidence_snippet": "",
        "reasoning_summary": "",
        "stop_draft_content": "",
        "manual": True,
        "manual_reason": f"{log['error_type']}: {log['summary']}",
        "degraded": True,
        "error": log,
    }
