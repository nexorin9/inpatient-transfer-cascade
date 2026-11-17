"""M 组：OpenAI-compatible 协议兼容性与 provider 可替换性（任务 26）。

配置全部由环境变量注入（monkeypatch），httpx.MockTransport 只在测试层注入；
两个不同 base_url 跑同一套协议断言，验证切换 base_url 即换服务、配置项不变。
"""
import json

import httpx
import pytest

from itc.his.redact import assert_no_phi
from itc.model_gateway.gateway import ModelGateway
from itc.model_gateway.prompt import INPUT_FIELDS

MODEL = "itc-judge"
KEY = "test-key"

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


def _env(monkeypatch, base, timeout="7.5"):
    monkeypatch.setenv("LLM_BASE_URL", base)
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", MODEL)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", timeout)


def _ok_response(result=None):
    result = result or VALID
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                }
            ]
        },
    )


def _assert_protocol(req):
    """同一套请求体 / 响应协议断言，供不同 base_url 复用。"""
    body = json.loads(req.content)
    assert req.method == "POST"
    assert str(req.url.path).endswith("/chat/completions")
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == MODEL
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    user = json.loads(body["messages"][1]["content"])
    assert set(user) == set(INPUT_FIELDS)
    assert "inpatient_no" not in user
    assert "20260001" not in json.dumps(body, ensure_ascii=False)
    assert_no_phi(body)


@pytest.mark.parametrize(
    "base",
    ["https://llm-a.internal/v1", "http://127.0.0.1:11434/v1"],
)
def test_provider_switch_by_base_url_only(monkeypatch, base):
    """切换 base_url 即换服务，配置项不变；同一套断言跑两个不同地址。"""
    _env(monkeypatch, base)
    seen = []

    def handler(req):
        seen.append(req)
        return _ok_response()

    # 不传 settings：base_url / api_key / model / timeout 全部来自环境变量
    gw = ModelGateway(transport=httpx.MockTransport(handler))
    result = gw.judge_order(ORDER, TRANSFER)
    assert len(seen) == 1
    req = seen[0]
    _assert_protocol(req)
    assert req.url.host in base
    assert result["judgement"] == "KEEP"
    assert result["manual"] is False and result["degraded"] is False
    # timeout 来自环境变量配置
    assert float(gw.client.timeout.read) == 7.5
    gw.close()


def test_config_all_from_env(monkeypatch):
    _env(monkeypatch, "https://llm-z.internal/v1", timeout="3.0")
    seen = []

    def handler(req):
        seen.append(req)
        return _ok_response()

    gw = ModelGateway(transport=httpx.MockTransport(handler))
    gw.judge_order(ORDER, TRANSFER)
    gw.close()
    assert gw.settings.llm_base_url == "https://llm-z.internal/v1"
    assert gw.settings.llm_model == MODEL
    assert gw.settings.llm_api_key == KEY
    assert json.loads(seen[0].content)["model"] == MODEL


def test_error_body_parsing(monkeypatch):
    _env(monkeypatch, "https://llm-e.internal/v1")

    def handler(req):
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    gw = ModelGateway(transport=httpx.MockTransport(handler))
    result = gw.judge_order(ORDER, TRANSFER)
    gw.close()
    assert result["judgement"] == "UNDETERMINED" and result["manual"] is True
    assert result["error"]["error_type"] == "http_4xx"
    assert "bad request" in result["error"]["summary"]


def test_unparsable_and_schema_response(monkeypatch):
    _env(monkeypatch, "https://llm-u.internal/v1")

    def h1(req):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "<<not json>>"}}]}
        )

    gw = ModelGateway(transport=httpx.MockTransport(h1))
    r1 = gw.judge_order(ORDER, TRANSFER)
    gw.close()
    assert r1["error"]["error_type"] == "unparsable"
    assert r1["judgement"] == "UNDETERMINED" and r1["manual"] is True

    def h2(req):
        bad = dict(VALID, judgement="MAYBE")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(bad)}}]}
        )

    gw = ModelGateway(transport=httpx.MockTransport(h2))
    r2 = gw.judge_order(ORDER, TRANSFER)
    gw.close()
    assert r2["error"]["error_type"] == "schema"
    assert r2["judgement"] == "UNDETERMINED" and r2["manual"] is True
