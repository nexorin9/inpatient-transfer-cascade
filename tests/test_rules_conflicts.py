"""R 组：执行科室硬矛盾判定（任务 10）。

覆盖四类边界：正常（一致 / 映射可解释）、缺映射、映射存在但语义不同、
规则文件损坏；硬矛盾 manual=True 且零自动处置写请求。
"""
from __future__ import annotations

from itc.domain.models import RuleHit
from itc.rules.conflicts import (
    RULE_CONSISTENT,
    RULE_EXPLAINED,
    RULE_HARD_CONFLICT,
    judge_execution_site,
)
from itc.rules.dept_map import load_dept_map

FROM = "<DEPT_CODE_FROM>"
TO = "<DEPT_CODE_TO>"


def _order(exec_code):
    return {
        "order_no": "<O1>", "drug_name": "<药名>", "content": "<内容>",
        "route": "口服", "frequency": "qd",
        "execute_dept_code": exec_code, "start_at": "2026-01-01T00:00:00+08:00",
    }


def _transfer(to_code=TO):
    return {
        "transfer_no": "<T1>", "inpatient_no": "<INPATIENT_NO>",
        "from_dept_code": FROM, "to_dept_code": to_code,
        "from_dept_name": "<原科室名>", "to_dept_name": "<目标科室名>",
        "ward_code": "<WARD_CODE>", "transfer_at": "2026-01-01T09:00:00+08:00",
    }


def test_consistent_site_is_not_manual():
    hit = judge_execution_site(_order(TO), _transfer(), load_dept_map())
    assert isinstance(hit, RuleHit)
    assert hit.rule_id == RULE_CONSISTENT
    assert hit.manual is False
    assert "execute_dept_code" in hit.fields and "to_dept_code" in hit.fields


def test_default_map_explains_difference():
    hit = judge_execution_site(_order(FROM), _transfer(), load_dept_map())
    assert hit.rule_id == RULE_EXPLAINED
    assert hit.manual is False
    assert "dept_map" in hit.fields


def test_missing_mapping_is_hard_conflict_and_zero_writes():
    class FakeWriter:
        def __init__(self):
            self.calls = 0

        def write_execution_site(self, **kwargs):
            self.calls += 1
            return {"ok": True}

    writer = FakeWriter()
    hit = judge_execution_site(_order("GHOST_DEPT"), _transfer(), load_dept_map())
    assert hit.rule_id == RULE_HARD_CONFLICT
    assert hit.manual is True

    def dispose(h):
        # 编排：只有 manual=False 的判定才进入自动写回路径
        if h.manual:
            return None
        return writer.write_execution_site(order_no="<O1>")

    assert dispose(hit) is None
    assert writer.calls == 0


def test_mapping_exists_but_semantics_differ_is_hard_conflict():
    # 条目在（有 canonical_name），但既不声明等价也不归并到新病区 -> 语义不同
    dm = {
        "mappings": {
            FROM: {"canonical_name": "呼吸内科", "equivalent_to": [], "canonical_code": "OTHER"},
            TO: {"canonical_name": "呼吸内科病区", "equivalent_to": [], "canonical_code": TO},
        }
    }
    hit = judge_execution_site(_order(FROM), _transfer(), dm)
    assert hit.rule_id == RULE_HARD_CONFLICT
    assert hit.manual is True


def test_corrupted_dept_map_file_still_conservative(tmp_path):
    bad = tmp_path / "dept_map.json"
    bad.write_text("NOT_JSON", encoding="utf-8")
    hit = judge_execution_site(_order(FROM), _transfer(), load_dept_map(str(bad)))
    assert hit.manual is True
    assert hit.rule_id == RULE_HARD_CONFLICT


def test_every_judgement_returns_explainable_rule_hit():
    for exec_code in (TO, FROM, "GHOST_DEPT"):
        hit = judge_execution_site(_order(exec_code), _transfer(), load_dept_map())
        assert isinstance(hit, RuleHit)
        assert hit.rule_id
        assert hit.fields
        assert isinstance(hit.reason, str) and hit.reason
