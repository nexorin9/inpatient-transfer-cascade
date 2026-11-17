"""M 组：结构化输出 Schema / 提示模板 / 出网字段边界（任务 11）。

协议替身（httpx.MockTransport）只在测试层注入，产品代码不为替身改写。
"""
import json

import httpx
import pytest

from itc.config import get_settings
from itc.his.redact import assert_no_phi
from itc.model_gateway import schema
from itc.model_gateway.gateway import ModelGateway, assert_request_boundary
from itc.model_gateway.prompt import (
    INPUT_FIELDS,
    SYSTEM_PROMPT,
    build_judgement_input,
    build_messages,
)

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
    "start_at": "2026-01-01T00:00:00+08:00",
}
TRANSFER = {
    "transfer_no": "T-1",
    "inpatient_no": "20260001",
    "from_dept_code": "A",
    "to_dept_code": "B",
    "from_dept_name": "原科室",
    "to_dept_name": "目标科室",
    "ward_code": "W-1",
    "transfer_at": "2026-01-01T09:00:00+08:00",
}
VALID = {
    "order_no": "O-1",
    "judgement": "KEEP",
    "confidence": 0.8,
    "evidence_fields": ["route", "frequency"],
    "evidence_snippet": "静滴 qd 长期医嘱",
    "reasoning_summary": "转科后仍属新病区常规用药",
    "stop_draft_content": "",
}


def _settings():
    return get_settings(
        llm_base_url=BASE,
        llm_api_key=KEY,
        llm_model=MODEL,
        llm_timeout_seconds=5.0,
    )


def test_schema_accepts_valid_and_all_four_judgements():
    for j in ("KEEP", "CHANGE_SITE", "NEED_STOP_DRAFT", "UNDETERMINED"):
        assert schema.validate_judgement(dict(VALID, judgement=j))["judgement"] == j
    for c in (0, 1, 0.5):  # confidence 含边界
        schema.validate_judgement(dict(VALID, confidence=c))
    # stop_draft_content 可空：缺省 / null / 空串
    p = dict(VALID)
    p.pop("stop_draft_content")
    schema.validate_judgement(p)
    schema.validate_judgement(dict(VALID, stop_draft_content=None))


def test_schema_rejects_bad_judgement_missing_fields_out_of_range_confidence():
    # judgement 四值互斥，四值外取值一律拒绝
    for bad in ("MAYBE", "keep", "KEEP|CHANGE_SITE", "", "STOP"):
        with pytest.raises(schema.JudgementSchemaError):
            schema.validate_judgement(dict(VALID, judgement=bad))
    # 缺字段
    for key in (
        "order_no", "judgement", "confidence", "evidence_fields",
        "evidence_snippet", "reasoning_summary",
    ):
        p = dict(VALID)
        p.pop(key)
        with pytest.raises(schema.JudgementSchemaError):
            schema.validate_judgement(p)
    # confidence 越界
    for c in (-0.1, 1.1, 2, -1):
        with pytest.raises(schema.JudgementSchemaError):
            schema.validate_judgement(dict(VALID, confidence=c))
    # evidence_fields 类型错
    with pytest.raises(schema.JudgementSchemaError):
        schema.validate_judgement(dict(VALID, evidence_fields="route"))


def test_system_prompt_is_constrained_task_and_messages_parseable():
    assert "转科后" in SYSTEM_PROMPT and "新病区" in SYSTEM_PROMPT
    for j in ("KEEP", "CHANGE_SITE", "NEED_STOP_DRAFT", "UNDETERMINED"):
        assert j in SYSTEM_PROMPT
    assert "JSON" in SYSTEM_PROMPT
    msgs = build_messages(ORDER, TRANSFER)
    assert isinstance(msgs, list) and len(msgs) == 2
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == SYSTEM_PROMPT
    assert msgs[1]["role"] == "user"
    payload = json.loads(msgs[1]["content"])
    assert set(payload) == set(INPUT_FIELDS)
    assert payload["order_no"] == "O-1"


def test_judgement_input_only_digest_and_order_fields():
    payload = build_judgement_input(ORDER, TRANSFER)
    assert set(payload) == {
        "inpatient_digest", "order_no", "content", "drug_name",
        "route", "frequency", "execute_dept_code",
    }
    assert payload["inpatient_digest"] != "20260001"
    assert len(payload["inpatient_digest"]) == 12
    assert "inpatient_no" not in payload
    for bad in ("name", "id_no", "phone", "contact"):
        assert bad not in payload
    assert_request_boundary(payload)  # 断言式边界检查通过


def test_assert_request_boundary_rejects_phi_and_extra_fields():
    base = build_judgement_input(ORDER, TRANSFER)
    for key in ("name", "id_no", "phone", "contact", "id_card"):
        with pytest.raises(ValueError):
            assert_request_boundary(dict(base, **{key: "x"}))
    with pytest.raises(ValueError):
        assert_request_boundary(dict(base, extra="x"))
    with pytest.raises(ValueError):
        assert_request_boundary(dict(base, inpatient_no="20260001"))


def test_request_body_boundary_and_response_parsed_by_schema():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(VALID, ensure_ascii=False)
                        }
                    }
                ]
            },
        )

    gw = ModelGateway(settings=_settings(), transport=httpx.MockTransport(handler))
    result = gw.judge_order(ORDER, TRANSFER)
    gw.close()

    assert result["judgement"] == "KEEP" and result["manual"] is False
    schema.validate_judgement(result)  # 返回值可解析为 schema
    body = json.loads(seen[0].content)
    assert seen[0].method == "POST"
    assert str(seen[0].url.path).endswith("/chat/completions")
    assert body["model"] == MODEL
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    user = json.loads(body["messages"][1]["content"])
    assert set(user) == set(INPUT_FIELDS)
    assert "20260001" not in json.dumps(body, ensure_ascii=False)  # 住院号原文不出网
    assert_no_phi(body)
