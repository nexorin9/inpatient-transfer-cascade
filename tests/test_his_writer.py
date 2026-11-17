"""H 组：HIS 出站写 2 类接口、幂等键、幂等去重与写路径白名单（任务 9）。"""
import json

import httpx
import pytest

from itc.graph.idempotency import idempotency_key
from itc.his import errors
from itc.his.client import HisClient
from itc.his.writer import WRITABLE_PATHS, HisWriter, assert_writable

BASE = "https://his.test"
TOKEN = "test-token"


def _client(handler):
    return HisClient(
        base_url=BASE, token=TOKEN, transport=httpx.MockTransport(handler)
    )


class _Registry:
    """内存 registry 替身：duck typing 实现 check / record。"""

    def __init__(self):
        self.rows = {}

    def check(self, key):
        return self.rows.get(key)

    def record(self, key, **kw):
        self.rows[key] = kw


def _exec_kwargs():
    return dict(
        order_id="O-1",
        inpatient_no="20260001",
        order_no="O-1",
        execute_dept_code="A",
        execute_dept_name="目标科室",
        effective_from="2026-01-01T09:00:00+08:00",
        transfer_no="T-1",
    )


def test_write_execution_site_contract():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    res = HisWriter(c).write_execution_site(**_exec_kwargs())
    assert len(seen) == 1
    q = seen[0]
    assert q.method == "POST"
    assert q.url.path == "/api/v1/orders/O-1/execution-site"
    assert q.headers["authorization"] == f"Bearer {TOKEN}"
    k = _exec_kwargs()
    expected = idempotency_key(
        k["transfer_no"], k["order_no"], k["execute_dept_code"], k["effective_from"]
    )
    assert q.headers["idempotency-key"] == expected
    assert json.loads(q.content) == {
        "inpatient_no": "20260001",
        "order_no": "O-1",
        "execute_dept_code": "A",
        "execute_dept_name": "目标科室",
        "effective_from": "2026-01-01T09:00:00+08:00",
        "reason": "TRANSFER_CASCADE",
        "transfer_no": "T-1",
    }
    assert res["ok"] is True and res["request_sent"] is True
    c.close()


def test_reason_override():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={})

    c = _client(handler)
    kw = _exec_kwargs()
    kw["reason"] = "MANUAL"
    HisWriter(c).write_execution_site(**kw)
    assert json.loads(seen[0].content)["reason"] == "MANUAL"
    c.close()


def test_create_stop_draft_contract():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    res = HisWriter(c).create_stop_draft(
        inpatient_no="20260001",
        order_no="O-9",
        content="停嘱草稿正文",
        source_transfer_no="T-9",
    )
    assert len(seen) == 1
    q = seen[0]
    assert q.method == "POST"
    assert q.url.path == "/api/v1/orders/drafts"
    assert q.headers["authorization"] == f"Bearer {TOKEN}"
    assert "idempotency-key" not in q.headers  # 草稿不要求幂等键头
    assert json.loads(q.content) == {
        "inpatient_no": "20260001",
        "order_no": "O-9",
        "draft_type": "STOP",
        "status": "DRAFT",
        "content": "停嘱草稿正文",
        "source_transfer_no": "T-9",
    }
    assert res["ok"] is True
    c.close()


def test_idempotency_second_call_skips_post():
    # 幂等复验：同一转科单重复提交，MockTransport 仅收到 1 次 POST。
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    w = HisWriter(c, registry=_Registry())
    r1 = w.write_execution_site(**_exec_kwargs())
    r2 = w.write_execution_site(**_exec_kwargs())
    assert len(seen) == 1
    assert r1["ok"] and r1["idempotent_hit"] is False
    assert r2["idempotent_hit"] is True and r2["request_sent"] is False
    c.close()


def test_stop_draft_idempotency_skips_post():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={})

    c = _client(handler)
    w = HisWriter(c, registry=_Registry())
    w.create_stop_draft(
        inpatient_no="20260001", order_no="O-9", content="x", source_transfer_no="T-9"
    )
    w.create_stop_draft(
        inpatient_no="20260001", order_no="O-9", content="x", source_transfer_no="T-9"
    )
    assert len(seen) == 1
    c.close()


def test_registry_none_is_allowed():
    # registry 可为 None：不做进程内抑制，但不影响正常写回。
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={})

    c = _client(handler)
    w = HisWriter(c)
    w.write_execution_site(**_exec_kwargs())
    w.write_execution_site(**_exec_kwargs())
    assert len(seen) == 2
    c.close()


def test_writable_paths_whitelist():
    assert set(WRITABLE_PATHS) == {
        "/api/v1/orders/{order_id}/execution-site",
        "/api/v1/orders/drafts",
    }
    assert_writable("/api/v1/orders/drafts")
    assert_writable("/api/v1/orders/O-1/execution-site")
    with pytest.raises(errors.NonRetryableHisError):
        assert_writable("/api/v1/orders/O-1/submit")
    with pytest.raises(errors.NonRetryableHisError):
        assert_writable("/api/v1/orders/O-1/cancel")


def test_no_submit_or_sign_call_in_writer_source(project_root):
    # 静态护栏：writer.py 源码不存在提交 / 签名医嘱的调用路径。
    src = (project_root / "src/itc/his/writer.py").read_text(encoding="utf-8")
    low = src.lower()
    assert "submit" not in low
    assert "sign" not in low
