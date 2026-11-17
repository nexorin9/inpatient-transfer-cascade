"""H 组：在执行医嘱资源读取与过滤参数契约（任务 17）。"""
import httpx
import pytest

from itc.domain.models import ExecutingOrder, FieldMissingError
from itc.his.client import HisClient
from itc.his.reader import HisReader

BASE = "https://his.test"
TOKEN = "test-token"


def _client(handler):
    return HisClient(
        base_url=BASE, token=TOKEN, transport=httpx.MockTransport(handler)
    )


def _order(no, dept, start):
    return {
        "order_no": no,
        "content": "医嘱内容",
        "drug_name": "药品名",
        "route": "口服",
        "frequency": "qd",
        "execute_dept_code": dept,
        "start_at": start,
    }


def test_read_executing_orders_contract():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(
            200,
            json={"items": [_order("O1", "A", "2026-01-01T00:00:00+08:00")]},
        )

    c = _client(handler)
    orders = HisReader(c).read_executing_orders("20260001")
    q = seen[0]
    assert q.method == "GET"
    assert q.url.path == "/api/v1/inpatients/20260001/orders"
    assert dict(q.url.params) == {"status": "executing"}
    assert q.headers["authorization"] == f"Bearer {TOKEN}"
    assert len(orders) == 1 and isinstance(orders[0], ExecutingOrder)
    assert orders[0].order_no == "O1" and orders[0].execute_dept_code == "A"
    c.close()


def test_missing_field_raises_no_defaults():
    bad = _order("O1", "A", "2026-01-01T00:00:00+08:00")
    bad.pop("start_at")

    def handler(req):
        return httpx.Response(200, json={"items": [bad]})

    c = _client(handler)
    with pytest.raises(FieldMissingError):
        HisReader(c).read_executing_orders("20260001")
    c.close()


@pytest.mark.parametrize(
    "bad", ["", "  ", "2026 0001", "2026000a", "<INPATIENT_NO>", "2026-0001"]
)
def test_invalid_inpatient_no_rejects_before_request(bad):
    # 住院号空 / 含空格 / 含非数字字符时报错且不发请求。
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"items": []})

    c = _client(handler)
    with pytest.raises(ValueError):
        HisReader(c).read_executing_orders(bad)
    assert seen == [], "非法住院号不应发出任何请求"
    c.close()


def test_group_orders_by_dept_and_sort():
    orders = [
        ExecutingOrder.from_dict(_order("A2", "A", "2026-01-03T00:00:00+08:00")),
        ExecutingOrder.from_dict(_order("B1", "B", "2026-01-02T00:00:00+08:00")),
        ExecutingOrder.from_dict(_order("A1", "A", "2026-01-01T00:00:00+08:00")),
    ]
    g = HisReader.group_orders_by_dept(orders)
    assert list(g.keys()) == ["A", "B"]
    # 组内按 start_at 升序
    assert [o.order_no for o in g["A"]] == ["A1", "A2"]
    assert [o.order_no for o in g["B"]] == ["B1"]


def test_empty_orders_ok():
    def handler(req):
        return httpx.Response(200, json={"items": []})

    c = _client(handler)
    assert HisReader(c).read_executing_orders("20260001") == []
    c.close()
