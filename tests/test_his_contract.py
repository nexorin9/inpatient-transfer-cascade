"""H 组：HIS 出站契约测试（任务 9 / 20）。

断言 URL 路径 / 方法 / 查询参数 / Authorization / Idempotency-Key / body 全字段，
以及出网前 PHI 边界与日志脱敏。
"""
import json

import httpx
import pytest

from itc.graph.idempotency import idempotency_key
from itc.his.client import HisClient
from itc.his.reader import HisReader
from itc.his.redact import assert_no_phi, digest_inpatient_no, redact_log
from itc.his.writer import WRITABLE_PATHS, HisWriter

BASE = "https://his.test"
TOKEN = "test-token"


def _client(handler):
    return HisClient(
        base_url=BASE, token=TOKEN, transport=httpx.MockTransport(handler)
    )


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
ORDER = {
    "order_no": "O-1",
    "content": "医嘱内容",
    "drug_name": "药品名",
    "route": "口服",
    "frequency": "qd",
    "execute_dept_code": "A",
    "start_at": "2026-01-01T00:00:00+08:00",
}
SPECIMEN = {
    "specimen_no": "S-1",
    "test_name": "血常规",
    "collect_dept_code": "A",
    "status": "pending",
}
CHARGE = {
    "category": "bed",
    "fee_date": "2026-01-01",
    "amount": "45.00",
    "dept_code": "A",
}


def test_all_read_endpoints_methods_params_and_auth():
    seen = []

    def router(req):
        seen.append(req)
        p = req.url.path
        if p == "/api/v1/transfers/T-1":
            return httpx.Response(200, json=TRANSFER)
        if p == "/api/v1/transfers":
            return httpx.Response(200, json={"items": [TRANSFER]})
        if p.endswith("/orders"):
            return httpx.Response(200, json={"items": [ORDER]})
        if p.endswith("/specimens"):
            return httpx.Response(200, json={"items": [SPECIMEN]})
        if p.endswith("/charges"):
            return httpx.Response(200, json={"items": [CHARGE]})
        return httpx.Response(404, json={})

    c = _client(router)
    r = HisReader(c)
    r.read_transfer("T-1")
    r.read_today_transfers("W-1", "2026-01-01")
    r.read_executing_orders("20260001")
    r.read_pending_specimens("20260001")
    r.read_bed_charges("20260001")

    assert [q.url.path for q in seen] == [
        "/api/v1/transfers/T-1",
        "/api/v1/transfers",
        "/api/v1/inpatients/20260001/orders",
        "/api/v1/inpatients/20260001/specimens",
        "/api/v1/inpatients/20260001/charges",
    ]
    assert all(q.method == "GET" for q in seen)
    assert all(q.headers["authorization"] == f"Bearer {TOKEN}" for q in seen)
    assert dict(seen[0].url.params) == {}
    assert dict(seen[1].url.params) == {"ward": "W-1", "date": "2026-01-01"}
    assert dict(seen[2].url.params) == {"status": "executing"}
    assert dict(seen[3].url.params) == {"status": "pending"}
    assert dict(seen[4].url.params) == {"category": "bed"}
    c.close()


def test_write_contract_full_body_and_header():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    w = HisWriter(c)
    w.write_execution_site(
        order_id="O-1",
        inpatient_no="20260001",
        order_no="O-1",
        execute_dept_code="A",
        execute_dept_name="目标科室",
        effective_from="2026-01-01T09:00:00+08:00",
        transfer_no="T-1",
    )
    w.create_stop_draft(
        inpatient_no="20260001",
        order_no="O-9",
        content="草稿",
        source_transfer_no="T-9",
    )

    q1, q2 = seen
    assert q1.method == "POST" and q2.method == "POST"
    assert q1.url.path == "/api/v1/orders/O-1/execution-site"
    assert q2.url.path == "/api/v1/orders/drafts"
    assert q1.headers["authorization"] == q2.headers["authorization"] == f"Bearer {TOKEN}"
    expected = idempotency_key("T-1", "O-1", "A", "2026-01-01T09:00:00+08:00")
    assert q1.headers["idempotency-key"] == expected
    assert json.loads(q1.content) == {
        "inpatient_no": "20260001",
        "order_no": "O-1",
        "execute_dept_code": "A",
        "execute_dept_name": "目标科室",
        "effective_from": "2026-01-01T09:00:00+08:00",
        "reason": "TRANSFER_CASCADE",
        "transfer_no": "T-1",
    }
    assert json.loads(q2.content) == {
        "inpatient_no": "20260001",
        "order_no": "O-9",
        "draft_type": "STOP",
        "status": "DRAFT",
        "content": "草稿",
        "source_transfer_no": "T-9",
    }
    c.close()


def test_writable_paths_only_two():
    assert len(WRITABLE_PATHS) == 2
    assert set(WRITABLE_PATHS) == {
        "/api/v1/orders/{order_id}/execution-site",
        "/api/v1/orders/drafts",
    }


def test_no_phi_in_write_bodies():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={})

    c = _client(handler)
    w = HisWriter(c)
    w.write_execution_site(
        order_id="O-1",
        inpatient_no="20260001",
        order_no="O-1",
        execute_dept_code="A",
        execute_dept_name="目标科室",
        effective_from="x",
        transfer_no="T-1",
    )
    w.create_stop_draft(
        inpatient_no="20260001", order_no="O-9", content="x", source_transfer_no="T-9"
    )
    for q in seen:
        body = json.loads(q.content)
        assert_no_phi(body)  # 通过即表示不含 name / id_no / phone / contact
        for bad in ("name", "id_no", "phone", "contact"):
            assert bad not in body
    c.close()


def test_assert_no_phi_rejects_phi_keys():
    for key in ("name", "id_no", "phone", "contact", "id_card", "mobile"):
        with pytest.raises(ValueError):
            assert_no_phi({"inpatient_no": "20260001", key: "x"})
    # 业务字段 execute_dept_name / inpatient_no 不受影响（精确匹配而非子串匹配）。
    assert_no_phi(
        {"inpatient_no": "20260001", "execute_dept_name": "目标科室", "order_no": "O-1"}
    )


def test_digest_and_redact_log():
    d = digest_inpatient_no("20260001")
    assert len(d) == 12 and d != "20260001"
    out = redact_log(
        {
            "inpatient_no": "20260001",
            "name": "张三",
            "id_no": "X",
            "phone": "1",
            "node": "fetch",
        }
    )
    assert out["inpatient_no"] == d
    assert out["name"] == "[REDACTED]"
    assert out["id_no"] == "[REDACTED]"
    assert out["phone"] == "[REDACTED]"
    assert out["node"] == "fetch"
    nested = redact_log({"nested": {"name": "x", "inpatient_no": "20260001"}})
    assert nested["nested"] == {"name": "[REDACTED]", "inpatient_no": d}
