"""M 组：五类错误语义 / 重试策略 / 待人工判定映射 / 错误日志（任务 24）。"""
import json

import httpx

from itc.config import get_settings
from itc.model_gateway import errors
from itc.model_gateway.gateway import ModelGateway

BASE = "https://llm.test/v1"
KEY = "test-key"
MODEL = "itc-judge"

ORDER = {
    "order_no": "O-1",
    "content": "氨溴索注射液 30mg 静滴",
    "drug_name": "氨溴索",
    "route": "静滴",
    "frequency": "qd",
    "execute_dept_code": "A",
}
TRANSFER = {"transfer_no": "T-1", "inpatient_no": "20260001", "to_dept_code": "B"}
VALID = {
    "order_no": "O-1",
    "judgement": "KEEP",
    "confidence": 0.8,
    "evidence_fields": ["route"],
    "evidence_snippet": "静滴 qd",
    "reasoning_summary": "仍属新病区",
    "stop_draft_content": "",
}


def _settings():
    return get_settings(
        llm_base_url=BASE,
        llm_api_key=KEY,
        llm_model=MODEL,
        llm_timeout_seconds=5.0,
    )


def _gw(handler):
    return ModelGateway(settings=_settings(), transport=httpx.MockTransport(handler))


def test_five_error_types_are_distinguishable():
    classes = [
        errors.ModelTimeoutError("超时"),
        errors.ModelConnectionError("连接失败"),
        errors.ModelHTTPError("4xx", status_code=404),
        errors.ModelHTTPError("5xx", status_code=503),
        errors.ModelUnparsableError("不可解析"),
        errors.ModelSchemaError("校验失败"),
    ]
    types = [e.error_type for e in classes]
    # 超时 / 连接失败 / 4xx / 5xx / 不可解析 / 校验失败，六种情形各自可区分
    assert types == [
        "timeout", "connection", "http_4xx", "http_5xx", "unparsable", "schema"
    ]
    assert len(set(types)) == 6
    for e in classes:
        assert isinstance(e, errors.ModelError)


def test_is_retryable_only_connection_and_5xx():
    # 契约：连接失败与 5xx 可重试；4xx 与校验失败不重试
    assert errors.is_retryable(errors.ModelConnectionError("x")) is True
    assert errors.is_retryable(errors.ModelHTTPError("x", status_code=500)) is True
    assert errors.is_retryable(errors.ModelHTTPError("x", status_code=503)) is True
    assert errors.is_retryable(errors.ModelHTTPError("x", status_code=400)) is False
    assert errors.is_retryable(errors.ModelHTTPError("x", status_code=429)) is False
    assert errors.is_retryable(errors.ModelSchemaError("x")) is False
    # 超时 / 不可解析按保守策略不重试，快速降级到人工判定
    assert errors.is_retryable(errors.ModelTimeoutError("x")) is False
    assert errors.is_retryable(errors.ModelUnparsableError("x")) is False


def test_failed_judgement_is_always_manual_undetermined():
    excs = [
        errors.ModelTimeoutError("超时"),
        errors.ModelConnectionError("连接失败"),
        errors.ModelHTTPError("HTTP 400", status_code=400),
        errors.ModelHTTPError("HTTP 503", status_code=503),
        errors.ModelUnparsableError("不可解析"),
        errors.ModelSchemaError("校验失败"),
    ]
    for exc in excs:
        d = errors.failed_judgement("O-1", exc, latency_ms=12.5, retries=1)
        assert d["judgement"] == "UNDETERMINED"
        assert d["manual"] is True
        assert d["judgement"] not in ("KEEP", "CHANGE_SITE")
        assert d["order_no"] == "O-1"
        assert d["degraded"] is True
        log = d["error"]
        assert {"error_type", "latency_ms", "retries", "summary"}.issubset(log)
        assert log["latency_ms"] == 12.5 and log["retries"] == 1


def test_error_log_has_no_inpatient_original_or_order_text():
    exc = errors.ModelHTTPError("HTTP 503", status_code=503)
    log = errors.error_log(exc, latency_ms=3.0, retries=2)
    blob = json.dumps(log, ensure_ascii=False)
    assert "20260001" not in blob
    assert "氨溴索注射液 30mg 静滴" not in blob
    assert log["error_type"] == "http_5xx"
    assert log["retries"] == 2


def test_connection_failure_retried_at_most_twice():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=req)

    gw = _gw(handler)
    result = gw.judge_order(ORDER, TRANSFER)
    gw.close()
    assert calls["n"] == 3  # 1 次调用 + 2 次重试
    assert result["judgement"] == "UNDETERMINED" and result["manual"] is True
    assert result["error"]["error_type"] == "connection"
    assert result["error"]["retries"] == 2


def test_5xx_retried_at_most_twice_with_error_body():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"message": "service unavailable"}})

    gw = _gw(handler)
    result = gw.judge_order(ORDER, TRANSFER)
    gw.close()
    assert calls["n"] == 3
    assert result["error"]["error_type"] == "http_5xx"
    assert "service unavailable" in result["error"]["summary"]  # 错误体解析
    assert result["manual"] is True


def test_4xx_schema_unparsable_timeout_not_retried():
    for kind in ("4xx", "schema", "unparsable", "timeout"):
        calls = {"n": 0}

        def handler(req, kind=kind):
            calls["n"] += 1
            if kind == "4xx":
                return httpx.Response(400, json={"error": {"message": "bad request"}})
            if kind == "schema":
                bad = dict(VALID, judgement="MAYBE")
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": json.dumps(bad)}}]},
                )
            if kind == "unparsable":
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "not-json"}}]},
                )
            raise httpx.ReadTimeout("timeout", request=req)

        gw = _gw(handler)
        result = gw.judge_order(ORDER, TRANSFER)
        gw.close()
        assert calls["n"] == 1, kind
        assert result["judgement"] == "UNDETERMINED" and result["manual"] is True
        assert result["error"]["retries"] == 0
