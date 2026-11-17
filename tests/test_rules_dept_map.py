"""R 组：科室映射表加载与解析（任务 10）。

覆盖：默认目录可维护、自定义 JSON 生效、缺映射不猜、规则文件损坏保守、
实体归一化、缺映射场景零自动处置写请求（假 writer 计数）。
"""
from __future__ import annotations

import json

from itc.domain.models import ExecutingOrder
from itc.rules.dept_map import (
    entity_to_dict,
    iter_mappings,
    load_dept_map,
    resolve_dept,
)


def test_default_catalog_loads_and_resolves():
    dm = load_dept_map()
    assert isinstance(dm, dict)
    assert "mappings" in dm
    name = resolve_dept("<DEPT_CODE_FROM>", dm)
    assert isinstance(name, str) and name
    assert resolve_dept("NO_SUCH_DEPT_CODE", dm) is None


def test_info_dept_can_maintain_custom_json(tmp_path):
    custom = {
        "version": "2.0",
        "mappings": {
            "D_A": {"canonical_name": "心内科", "equivalent_to": ["D_B"]},
            "D_B": {"canonical_name": "心内科病区", "equivalent_to": []},
        },
    }
    p = tmp_path / "dept_map.json"
    p.write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")
    dm = load_dept_map(str(p))
    assert dm["version"] == "2.0"
    assert resolve_dept("D_A", dm) == "心内科"
    assert resolve_dept("D_MISSING", dm) is None
    # 传目录也应能定位到 dept_map.json
    assert resolve_dept("D_A", load_dept_map(tmp_path)) == "心内科"


def test_iter_mappings_accepts_plain_dict_form():
    assert iter_mappings({"K": "普通外科", "version": "9"}) == {"K": "普通外科"}
    assert resolve_dept("K", {"K": "普通外科", "version": "9"}) == "普通外科"


def test_entity_to_dict_normalizes_domain_object():
    order = ExecutingOrder.from_dict({
        "order_no": "<O1>", "content": "<内容>", "drug_name": "<药名>",
        "route": "口服", "frequency": "qd",
        "execute_dept_code": "<DEPT_CODE_FROM>", "start_at": "2026-01-01T00:00:00+08:00",
    })
    d = entity_to_dict(order)
    assert d["execute_dept_code"] == "<DEPT_CODE_FROM>"
    assert entity_to_dict({"a": 1}) == {"a": 1}
    assert entity_to_dict(None) == {}


def test_missing_mapping_yields_zero_auto_write_requests():
    """缺映射 -> resolve 为 None -> 编排不发任何自动处置写请求。"""
    class FakeWriter:
        def __init__(self):
            self.calls = 0

        def write_execution_site(self, **kwargs):
            self.calls += 1
            return {"ok": True}

    writer = FakeWriter()
    dm = load_dept_map()

    def auto_dispose(exec_code):
        # 只有映射可解析（不缺映射）才允许自动改执行地点；缺映射不猜、转人工
        if resolve_dept(exec_code, dm) is None:
            return None
        return writer.write_execution_site(execute_dept_code=exec_code)

    assert auto_dispose("UNKNOWN_DEPT_CODE") is None
    assert writer.calls == 0
    assert auto_dispose("<DEPT_CODE_FROM>") is not None
    assert writer.calls == 1  # 对照组：有映射才发一次写请求


def test_corrupted_catalog_is_conservative(tmp_path):
    bad = tmp_path / "dept_map.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    dm = load_dept_map(str(bad))
    assert dm == {} or not iter_mappings(dm)
    assert resolve_dept("<DEPT_CODE_FROM>", dm) is None


def test_missing_catalog_file_is_conservative(tmp_path):
    dm = load_dept_map(str(tmp_path / "absent.json"))
    assert resolve_dept("<DEPT_CODE_FROM>", dm) is None
