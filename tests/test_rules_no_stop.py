"""R 组：不可停品类守门（任务 22）。

覆盖：药品名精确 / 类别前缀 / 院内编码三类命中，命中即草稿产出 0、
不进可写清单、标注为不可自动停嘱·需医师判断；未命中对照；目录缺失或
损坏保守按不可停处理并带 catalog_error 标记。
"""
from __future__ import annotations

from itc.domain.models import RuleHit
from itc.rules.no_stop import (
    RULE_ERROR,
    RULE_HIT,
    NoStopRuleHit,
    is_no_stop,
    load_no_stop_catalog,
)


class FakeDraftWriter:
    """只计数的假 writer（测试层替身，不出网、不落真实医嘱）。"""

    def __init__(self):
        self.calls = 0
        self.bodies = []

    def create_stop_draft(self, **body):
        self.calls += 1
        self.bodies.append(body)
        return {"status": "DRAFT"}


def _guard_plan(order, catalog=None):
    """真实守门编排：命中不可停 -> 草稿 0、不进可写清单；未命中才产草稿。"""
    writer = FakeDraftWriter()
    hit = is_no_stop(order, catalog)
    writable_list = []
    if hit is None:
        writer.create_stop_draft(
            order_no=order.get("order_no", ""), draft_type="STOP", status="DRAFT"
        )
        writable_list.append(order.get("order_no", ""))
    return writer, hit, writable_list


def _base_order(**overrides):
    o = {
        "order_no": "<O1>", "content": "0.9% 氯化钠注射液 静滴",
        "drug_name": "氯化钠注射液", "route": "静滴", "frequency": "qd",
        "execute_dept_code": "<DEPT_CODE_FROM>", "start_at": "2026-01-01T00:00:00+08:00",
        "hospital_code": "NS-OK-999",
    }
    o.update(overrides)
    return o


def test_exact_drug_name_hit_blocks_draft():
    writer, hit, writable = _guard_plan(_base_order(drug_name="输血用红细胞悬液"))
    assert isinstance(hit, NoStopRuleHit) and isinstance(hit, RuleHit)
    assert hit.rule_id == RULE_HIT
    assert hit.manual is True
    assert "不可自动停嘱·需医师判断" in hit.label
    assert "药品名精确" in hit.reason
    assert hit.catalog_error is False
    assert writer.calls == 0
    assert writable == []


def test_category_prefix_hit_blocks_draft():
    writer, hit, _ = _guard_plan(_base_order(content="抗菌药·注射用头孢曲松"))
    assert hit is not None
    assert "类别前缀" in hit.reason
    assert writer.calls == 0


def test_hospital_code_hit_blocks_draft():
    writer, hit, _ = _guard_plan(_base_order(hospital_code="NS-BLOOD-001"))
    assert hit is not None
    assert "院内编码" in hit.reason
    assert writer.calls == 0


def test_no_hit_allows_draft():
    writer, hit, writable = _guard_plan(_base_order())
    assert hit is None
    assert writer.calls == 1
    assert writable == ["<O1>"]
    assert writer.bodies[0]["status"] == "DRAFT"


def test_corrupted_catalog_is_conservative_with_catalog_error(tmp_path):
    bad = tmp_path / "no_stop_catalog.json"
    bad.write_text("{ broken", encoding="utf-8")
    writer, hit, writable = _guard_plan(_base_order(), catalog=str(bad))
    assert hit is not None
    assert hit.rule_id == RULE_ERROR
    assert hit.catalog_error is True
    assert "人工维护" in hit.reason
    assert "catalog_error" in hit.reason
    assert writer.calls == 0
    assert writable == []


def test_missing_catalog_is_conservative(tmp_path):
    writer, hit, _ = _guard_plan(_base_order(), catalog=str(tmp_path / "absent.json"))
    assert hit is not None
    assert hit.catalog_error is True
    assert writer.calls == 0


def test_default_catalog_loadable_and_hit_fields():
    cat = load_no_stop_catalog()
    assert cat.get("drug_names") and cat.get("category_prefixes") and cat.get("hospital_codes")
    hit = is_no_stop(_base_order(drug_name="人血白蛋白"), cat)
    assert hit is not None
    assert "drug_name" in hit.fields
