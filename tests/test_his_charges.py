"""H 组：床位费明细读取与容错（任务 18 下半）。"""
from decimal import Decimal

import httpx
import pytest

from itc.domain.models import BedCharge
from itc.his import errors
from itc.his.client import HisClient
from itc.his.reader import HisReader

BASE = "https://his.test"
TOKEN = "test-token"


def _client(handler):
    return HisClient(
        base_url=BASE, token=TOKEN, transport=httpx.MockTransport(handler)
    )


def _ch(amount, category="bed", date="2026-01-01", dept="A"):
    return {
        "category": category,
        "fee_date": date,
        "amount": amount,
        "dept_code": dept,
    }


def test_read_bed_charges_contract():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"items": [_ch("45.00")]})

    c = _client(handler)
    rows = HisReader(c).read_bed_charges("20260001")
    q = seen[0]
    assert q.method == "GET"
    assert q.url.path == "/api/v1/inpatients/20260001/charges"
    assert dict(q.url.params) == {"category": "bed"}
    assert q.headers["authorization"] == f"Bearer {TOKEN}"
    assert len(rows) == 1 and isinstance(rows[0], BedCharge)
    c.close()


def test_decimal_precision():
    def handler(req):
        return httpx.Response(200, json={"items": [_ch("45.005")]})

    c = _client(handler)
    r = HisReader(c)
    rows = r.read_bed_charges("20260001")
    amount = r.bed_charge_amount(rows[0])
    assert amount == Decimal("45.005")
    assert str(amount) == "45.005"  # 不引入浮点误差
    c.close()


def test_empty_and_negative_amounts_flagged():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "items": [
                    _ch("45.00"),
                    _ch(""),
                    _ch("-3.00"),
                    _ch("abc"),
                ]
            },
        )

    c = _client(handler)
    r = HisReader(c)
    rows = r.read_bed_charges("20260001")
    assert len(rows) == 4
    assert rows[0].needs_manual_check is False
    assert rows[1].needs_manual_check is True
    assert rows[2].needs_manual_check is True
    assert rows[3].needs_manual_check is True
    manual = r.manual_check_charges(rows)
    assert len(manual) == 3
    assert all("需人工核对" in x.manual_check_reason for x in manual)
    c.close()


def test_only_bed_category_counted():
    def handler(req):
        return httpx.Response(
            200,
            json={"items": [_ch("45.00"), _ch("10.00", category="meal")]},
        )

    c = _client(handler)
    rows = HisReader(c).read_bed_charges("20260001")
    assert len(rows) == 1 and rows[0].category == "bed"
    c.close()


def test_failure_is_not_empty_list():
    def handler(req):
        return httpx.Response(500, json={"e": "down"})

    c = _client(handler)
    with pytest.raises(errors.RetryableHisError):
        HisReader(c).read_bed_charges("20260001")
    c.close()


def test_empty_ok():
    def handler(req):
        return httpx.Response(200, json={"items": []})

    c = _client(handler)
    assert HisReader(c).read_bed_charges("20260001") == []
    c.close()
