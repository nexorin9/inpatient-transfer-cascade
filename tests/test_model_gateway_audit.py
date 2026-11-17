"""M 组：调用审计元数据落库与脱敏（任务 26）。"""
import hashlib
import json

import httpx

from itc.config import get_settings
from itc.model_gateway.audit import ModelAuditStore, digest_input
from itc.model_gateway.gateway import ModelGateway
from itc.model_gateway.prompt import build_judgement_input

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


def test_digest_is_over_redacted_input_only():
    payload = build_judgement_input(ORDER, TRANSFER)
    d = digest_input(payload)
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert len(d) == 64 and d == expected
    # 摘要不可逆，不含住院号原文与医嘱全文
    assert "20260001" not in d
    assert "氨溴索" not in d


def test_audit_row_fields_complete_and_no_originals(db_conn):
    store = ModelAuditStore(db_conn)
    payload = build_judgement_input(ORDER, TRANSFER)
    digest = digest_input(payload)
    store.record(
        model=MODEL, latency_ms=12.3, input_digest=digest,
        judgement="KEEP", degraded=False, retries=1,
    )
    rows = store.rows()
    assert len(rows) == 1
    r = rows[0]
    for f in (
        "model", "latency_ms", "input_digest", "judgement",
        "degraded", "retries", "created_at",
    ):
        assert f in r, f
    assert r["model"] == MODEL
    assert r["latency_ms"] == 12.3
    assert r["input_digest"] == digest
    assert r["judgement"] == "KEEP"
    assert r["degraded"] == 0
    assert r["retries"] == 1
    blob = json.dumps(r, ensure_ascii=False)
    assert "20260001" not in blob
    assert "氨溴索注射液 30mg 静滴" not in blob
    assert "氨溴索" not in blob


def test_gateway_success_records_audit(db_conn):
    store = ModelAuditStore(db_conn)

    def handler(req):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(VALID, ensure_ascii=False)}}
                ]
            },
        )

    gw = ModelGateway(settings=_settings(), transport=httpx.MockTransport(handler))
    result = gw.judge_order(ORDER, TRANSFER, session=store)
    gw.close()
    assert result["judgement"] == "KEEP"
    rows = store.rows()
    assert len(rows) == 1
    assert rows[0]["judgement"] == "KEEP"
    assert rows[0]["degraded"] == 0
    assert rows[0]["model"] == MODEL
    assert rows[0]["latency_ms"] >= 0
    assert len(rows[0]["input_digest"]) == 64


def test_gateway_failure_records_degraded_audit(db_conn):
    store = ModelAuditStore(db_conn)

    def handler(req):
        return httpx.Response(500, json={"error": {"message": "oops"}})

    gw = ModelGateway(settings=_settings(), transport=httpx.MockTransport(handler))
    result = gw.judge_order(ORDER, TRANSFER, session=store)
    gw.close()
    assert result["judgement"] == "UNDETERMINED" and result["manual"] is True
    rows = store.rows()
    assert len(rows) == 1
    assert rows[0]["judgement"] == "UNDETERMINED"
    assert rows[0]["degraded"] == 1
    assert rows[0]["retries"] == 2  # 5xx 重试 2 次
    blob = json.dumps(rows[0], ensure_ascii=False)
    assert "20260001" not in blob
