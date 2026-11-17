"""R 组：床位费归属一致性提示（任务 23）。

覆盖：口径声明（资产层未给换算规则，此处为一致性提示）、完全一致 / 归属存疑
两档、转科当天单列且不与前后合并、只提示不判违规、domain 对象入参。
"""
from __future__ import annotations

from itc.domain.models import RuleHit
from itc.rules.bed_fee import (
    BED_FEE_DISCLAIMER,
    DISCLAIMER,
    RULE_CONSISTENT,
    RULE_DOUBT,
    RULE_TRANSFER_DAY,
    bed_fee_hints,
)

FROM = "<DEPT_CODE_FROM>"
TO = "<DEPT_CODE_TO>"
DAY = "2026-01-01"


def _transfer(transfer_at="2026-01-01T09:00:00+08:00"):
    return {
        "transfer_no": "<T1>", "inpatient_no": "<INPATIENT_NO>",
        "from_dept_code": FROM, "to_dept_code": TO,
        "from_dept_name": "<原科室名>", "to_dept_name": "<目标科室名>",
        "ward_code": "<WARD_CODE>", "transfer_at": transfer_at,
    }


def _charge(fee_date, dept_code, amount="45.00"):
    return {"category": "bed", "fee_date": fee_date, "amount": amount, "dept_code": dept_code}


def test_disclaimer_present_in_module_and_every_hint():
    assert DISCLAIMER == "资产层未给换算规则，此处为一致性提示"
    assert BED_FEE_DISCLAIMER == DISCLAIMER
    hints = bed_fee_hints(
        [_charge("2025-12-31", FROM), _charge(DAY, FROM), _charge("2026-01-02", TO)],
        _transfer(),
    )
    assert hints
    for h in hints:
        assert isinstance(h, RuleHit)
        assert DISCLAIMER in h.reason
        assert "不判违规" in h.reason


def test_consistent_level():
    before = bed_fee_hints([_charge("2025-12-31", FROM)], _transfer())
    after = bed_fee_hints([_charge("2026-01-02", TO)], _transfer())
    for hints in (before, after):
        assert len(hints) == 1
        assert hints[0].rule_id == RULE_CONSISTENT
        assert "完全一致" in hints[0].reason
        assert hints[0].manual is False


def test_ownership_doubt_level():
    hints = bed_fee_hints([_charge("2025-12-31", "OTHER_DEPT")], _transfer())
    assert hints[0].rule_id == RULE_DOUBT
    assert "归属存疑" in hints[0].reason
    assert hints[0].manual is True


def test_transfer_day_is_single_line_not_merged():
    charges = [
        _charge("2025-12-31", FROM),
        _charge(DAY, FROM),
        _charge("2026-01-03", TO),
    ]
    hints = bed_fee_hints(charges, _transfer())
    assert len(hints) == len(charges)  # 每条独立提示，不与前后合并
    day_hints = [h for h in hints if h.rule_id == RULE_TRANSFER_DAY]
    assert len(day_hints) == 1  # 只有转科当天那行单列
    assert "单列提示" in day_hints[0].reason
    assert "不与转科前后合并" in day_hints[0].reason


def test_only_two_levels_and_manual_split():
    hints = bed_fee_hints([
        _charge("2025-12-31", FROM),
        _charge("2025-12-30", "OTHER"),
        _charge("2026-01-02", TO),
    ], _transfer())
    levels = set()
    for h in hints:
        assert ("完全一致" in h.reason) or ("归属存疑" in h.reason)
        if "完全一致" in h.reason:
            levels.add("完全一致")
            assert h.manual is False
        else:
            levels.add("归属存疑")
            assert h.manual is True
    assert levels == {"完全一致", "归属存疑"}


def test_transfer_time_missing_is_conservative():
    hints = bed_fee_hints([_charge("2025-12-31", FROM)], _transfer(transfer_at=""))
    assert hints[0].rule_id == RULE_DOUBT
    assert hints[0].manual is True
    assert "无法定位转科当天" in hints[0].reason


def test_accepts_domain_objects(sample_charges, sample_transfer):
    hints = bed_fee_hints(sample_charges, sample_transfer)
    assert len(hints) == len(sample_charges)
    assert all(isinstance(h, RuleHit) for h in hints)
    assert hints[0].rule_id == RULE_TRANSFER_DAY
