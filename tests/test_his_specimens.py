"""H 组：待送标本读取与容错（任务 18 上半）。"""
import httpx
import pytest

from itc.domain.models import Specimen
from itc.his import errors
from itc.his.client import HisClient
from itc.his.reader import HisReader

BASE = "https://his.test"
TOKEN = "test-token"


def _client(handler):
    return HisClient(
        base_url=BASE, token=TOKEN, transport=httpx.MockTransport(handler)
    )


def _sp(no, status):
    return {
        "specimen_no": no,
        "test_name": "血常规",
        "collect_dept_code": "A",
        "status": status,
    }


def test_read_pending_specimens_contract():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"items": [_sp("S1", "pending")]})

    c = _client(handler)
    rows = HisReader(c).read_pending_specimens("20260001")
    q = seen[0]
    assert q.method == "GET"
    assert q.url.path == "/api/v1/inpatients/20260001/specimens"
    assert dict(q.url.params) == {"status": "pending"}
    assert q.headers["authorization"] == f"Bearer {TOKEN}"
    assert len(rows) == 1 and isinstance(rows[0], Specimen)
    assert rows[0].specimen_no == "S1" and rows[0].status == "pending"
    c.close()


def test_only_pending_counted():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "items": [
                    _sp("S1", "pending"),
                    _sp("S2", "sent"),
                    _sp("S3", "collected"),
                ]
            },
        )

    c = _client(handler)
    rows = HisReader(c).read_pending_specimens("20260001")
    assert [r.specimen_no for r in rows] == ["S1"]
    c.close()


def test_non_pending_row_with_missing_fields_is_ignored():
    # 非 pending 行不计入，也不参与字段严格校验（脏行不拖垮整类资源）。
    dirty = {"specimen_no": "S2"}

    def handler(req):
        return httpx.Response(200, json={"items": [_sp("S1", "pending"), dirty]})

    c = _client(handler)
    rows = HisReader(c).read_pending_specimens("20260001")
    assert [r.specimen_no for r in rows] == ["S1"]
    c.close()


def test_failure_is_not_empty_list():
    # 单类资源取回失败不得当作空列表（供上层隔离）。
    def handler(req):
        return httpx.Response(503, json={"e": "down"})

    c = _client(handler)
    with pytest.raises(errors.RetryableHisError):
        HisReader(c).read_pending_specimens("20260001")
    c.close()


def test_empty_ok():
    def handler(req):
        return httpx.Response(200, json={"items": []})

    c = _client(handler)
    assert HisReader(c).read_pending_specimens("20260001") == []
    c.close()
