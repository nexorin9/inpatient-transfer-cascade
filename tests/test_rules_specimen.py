"""R 组：标本去向唯一性判定（任务 21）。

覆盖：候选唯一可自动、候选 >1 拒绝自动处置且零写请求、状态停在
awaiting_confirm 之前、人工待办含候选与原因、目录查表歧义 / 唯一样例。
"""
from __future__ import annotations

from itc.domain.models import STATES, RuleHit
from itc.rules.specimen_route import (
    RULE_AMBIGUOUS,
    RULE_UNIQUE,
    build_manual_todo,
    judge_specimen_route,
    load_specimen_routes,
)


def _specimen(test_name="<检验项目名 1>"):
    return {
        "specimen_no": "<S1>", "test_name": test_name,
        "collect_dept_code": "<DEPT_CODE_FROM>", "status": "pending",
    }


def _transfer():
    return {
        "to_dept_code": "<DEPT_CODE_TO>", "from_dept_code": "<DEPT_CODE_FROM>",
        "transfer_at": "2026-01-01T09:00:00+08:00",
    }


def test_unique_candidate_allows_auto_disposition():
    hit = judge_specimen_route(_specimen(), _transfer(), candidates=["<LAB_DEPT_CODE>"])
    assert isinstance(hit, RuleHit)
    assert hit.rule_id == RULE_UNIQUE
    assert hit.manual is False
    assert hit.candidates == ["<LAB_DEPT_CODE>"]
    assert hit.fields


def test_ambiguous_candidates_reject_auto_disposition_and_zero_writes():
    class FakeWriter:
        def __init__(self):
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return {"ok": True}

    writer = FakeWriter()
    hit = judge_specimen_route(_specimen(), _transfer(), candidates=["检验科", "院内标本站"])
    assert hit.rule_id == RULE_AMBIGUOUS
    assert hit.manual is True
    assert "检验科" in hit.candidates and "院内标本站" in hit.candidates
    assert "拒绝自动处置" in hit.reason

    def dispose(h):
        # 编排：manual=True 只产人工待办，不进写回路径、不推进到 awaiting_confirm
        if h.manual:
            return build_manual_todo(_specimen(), h)
        writer.post("/specimen-route")
        return {"state": "awaiting_confirm"}

    todo = dispose(hit)
    assert writer.calls == 0
    assert todo["manual"] is True
    assert todo["state"] == "rule_judged"
    assert todo["state"] != "awaiting_confirm"
    assert set(todo["candidates"]) == {"检验科", "院内标本站"}
    assert "人工" in todo["reason"]
    assert todo["rule_id"] == hit.rule_id


def test_unresolvable_candidate_is_conservative():
    hit = judge_specimen_route(_specimen(), _transfer(), candidates=[])
    assert hit.manual is True
    assert hit.rule_id == RULE_AMBIGUOUS
    assert "无法定位" in hit.reason


def test_catalog_lookup_ambiguous_sample():
    cat = load_specimen_routes()
    assert isinstance(cat.get("by_test_name"), dict)
    hit = judge_specimen_route(_specimen("血常规"), _transfer())
    assert hit.manual is True
    assert len(hit.candidates) > 1


def test_catalog_lookup_unique_sample():
    hit = judge_specimen_route(_specimen("凝血四项"), _transfer())
    assert hit.rule_id == RULE_UNIQUE
    assert hit.manual is False


def test_manual_todo_state_stays_before_awaiting_confirm():
    hit = judge_specimen_route(_specimen(), _transfer(), candidates=["A", "B"])
    todo = build_manual_todo(_specimen(), hit)
    assert STATES.index(todo["state"]) < STATES.index("awaiting_confirm")
    assert todo["ref"] == "<S1>"
    assert todo["rule_id"] == RULE_AMBIGUOUS
    assert hit.fields  # 依据字段可解释
