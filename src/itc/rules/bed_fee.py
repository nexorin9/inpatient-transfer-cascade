"""床位费归属一致性提示（只提示，不判违规）。

业务口径（spec §6）：床位费属医疗收费信息 14 类费用之一；住院诊疗信息含科别
与转科科别；但床位费归属如何与清单科别 / 转科科别换算，资产层未给规则
（经验推断），故本模块只做一致性提示，不替医院定口径、不写成处罚结论。

提示分两档：完全一致 / 归属存疑（需人工核对）；转科当天的床位费行单列提示，
不与转科前后合并。每条提示的 reason 都带口径声明。
"""
from __future__ import annotations

from itc.domain.models import RuleHit
from itc.rules.dept_map import entity_to_dict

DISCLAIMER = "资产层未给换算规则，此处为一致性提示"
BED_FEE_DISCLAIMER = DISCLAIMER

RULE_CONSISTENT = "BED_FEE_CONSISTENT"
RULE_DOUBT = "BED_FEE_OWNERSHIP_DOUBT"
RULE_TRANSFER_DAY = "BED_FEE_TRANSFER_DAY"


def _date_of(value):
    """取 ISO8601 的日期部分（前 10 位）；空值返回空串，不猜。"""
    s = str(value or "").strip()
    return s[:10] if len(s) >= 10 else ""


def _hint(rule_id, level, extra, fields):
    """构造一条一致性提示 RuleHit：reason 恒带口径声明与不判违规说明。"""
    reason = (
        f"{DISCLAIMER}；{level}；{extra}；仅作归属一致性提示，不判违规、不替医院定换算口径"
    )
    return RuleHit(
        rule_id=rule_id,
        fields=fields,
        reason=reason,
        manual=(rule_id != RULE_CONSISTENT),
        candidates=[],
    )


def bed_fee_hints(charges, transfer) -> list[RuleHit]:
    """对每条床位费明细产出一条一致性提示。

    期望归属：转科前（fee_date < 转科当天）归 from_dept_code，转科后归
    to_dept_code；转科当天单列提示，不与前后合并。转科时间缺失时无法定位
    当天，全部按归属存疑提示人工核对。
    """
    t = entity_to_dict(transfer)
    from_code = str(t.get("from_dept_code") or "")
    to_code = str(t.get("to_dept_code") or "")
    day = _date_of(t.get("transfer_at"))
    fields = ["fee_date", "dept_code", "amount", "transfer_at", "from_dept_code", "to_dept_code"]
    hints = []
    for charge in charges or []:
        c = entity_to_dict(charge)
        fee_date = str(c.get("fee_date") or "")
        fee_day = fee_date[:10]
        dept_code = str(c.get("dept_code") or "")
        amount = str(c.get("amount") or "")
        detail = f"费用日期 {fee_date}，归属科室 {dept_code}，金额 {amount}"

        if not day:
            hints.append(_hint(
                RULE_DOUBT, "归属存疑",
                f"{detail}；转科时间缺失，无法定位转科当天，需人工核对",
                fields,
            ))
            continue
        if fee_day == day:
            hints.append(_hint(
                RULE_TRANSFER_DAY, "归属存疑（转科当天单列）",
                f"{detail}；转科当天的床位费行单列提示，不与转科前后合并，需按医院口径人工核对",
                fields,
            ))
            continue
        before = fee_day < day
        expected = from_code if before else to_code
        if dept_code == expected:
            hints.append(_hint(
                RULE_CONSISTENT, "完全一致",
                f"{detail}；与{'转科前原科室' if before else '转科后新病区'} {expected} 一致",
                fields,
            ))
        else:
            hints.append(_hint(
                RULE_DOUBT, "归属存疑",
                f"{detail}；期望归属 {expected}（科别 / 转科科别），实际 {dept_code}，需人工核对",
                fields,
            ))
    return hints
