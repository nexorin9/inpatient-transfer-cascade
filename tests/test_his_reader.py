"""H 组：HIS 读客户端配置自检与转科单类资源读取（任务 8）。

httpx.MockTransport 只在测试层注入；真实断言 URL 路径 / 查询参数 /
Authorization 头与字段严格解析。
"""
import httpx
import pytest

from itc.config import ConfigurationError
from itc.domain.models import FieldMissingError, TransferOrder
from itc.his import errors
from itc.his.client import HisClient
from itc.his.reader import HisReader

BASE = "https://his.test"
TOKEN = "test-token"


def _client(handler):
    return HisClient(
        base_url=BASE, token=TOKEN, transport=httpx.MockTransport(handler)
    )


TRANSFER = {
    "transfer_no": "T-001",
    "inpatient_no": "20260001",
    "from_dept_code": "A",
    "to_dept_code": "B",
    "from_dept_name": "原科室",
    "to_dept_name": "目标科室",
    "ward_code": "W-1",
    "transfer_at": "2026-01-01T09:00:00+08:00",
}


def test_missing_credentials_raises(monkeypatch, tmp_path):
    # 启动自检：环境变量缺失时明确报错，不静默降级到本地替身。
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HIS_BASE_URL", raising=False)
    monkeypatch.delenv("HIS_API_TOKEN", raising=False)
    with pytest.raises(ConfigurationError):
        HisClient()


def test_empty_explicit_credentials_raises():
    with pytest.raises(ConfigurationError):
        HisClient(base_url="", token="")
    with pytest.raises(ConfigurationError):
        HisClient(base_url="https://his.test", token="   ")


def test_read_transfer_url_path_and_auth():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=TRANSFER)

    c = _client(handler)
    t = HisReader(c).read_transfer("T-001")
    q = seen[0]
    assert q.method == "GET"
    assert q.url.path == "/api/v1/transfers/T-001"
    assert q.headers["authorization"] == f"Bearer {TOKEN}"
    assert isinstance(t, TransferOrder)
    assert t.transfer_no == "T-001" and t.to_dept_code == "B"
    c.close()


def test_read_transfer_missing_field_raises():
    # 缺字段抛 FieldMissingError，不以默认值兜底。
    bad = dict(TRANSFER)
    bad.pop("transfer_at")

    def handler(req):
        return httpx.Response(200, json=bad)

    c = _client(handler)
    with pytest.raises(FieldMissingError):
        HisReader(c).read_transfer("T-001")
    c.close()


def test_read_today_transfers_params_and_auth():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"items": [TRANSFER]})

    c = _client(handler)
    rows = HisReader(c).read_today_transfers("W-1", "2026-01-01")
    q = seen[0]
    assert q.method == "GET"
    assert q.url.path == "/api/v1/transfers"
    assert dict(q.url.params) == {"ward": "W-1", "date": "2026-01-01"}
    assert q.headers["authorization"] == f"Bearer {TOKEN}"
    assert len(rows) == 1 and isinstance(rows[0], TransferOrder)
    c.close()


def test_today_transfers_empty_is_empty_list():
    def handler(req):
        return httpx.Response(200, json={"items": []})

    c = _client(handler)
    assert HisReader(c).read_today_transfers("W-1", "2026-01-01") == []
    c.close()


def test_read_transfer_http_error_is_failure_not_empty():
    # 失败与空结果必须区分：5xx 抛异常，供上层标记节点失败并隔离。
    def handler(req):
        return httpx.Response(500, json={"e": "down"})

    c = _client(handler)
    with pytest.raises(errors.RetryableHisError):
        HisReader(c).read_transfer("T-001")
    c.close()
