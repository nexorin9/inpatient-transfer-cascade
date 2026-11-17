"""判定提示模板与出网输入构造。

把「转科后这条长嘱是否仍属新病区」写成带输出约束的判定任务；出网输入只含
住院号摘要与医嘱白名单字段，且用 assert_no_phi 做断言式 PHI 边界检查。
"""
from __future__ import annotations

import json

from itc.his.redact import assert_no_phi, digest_inpatient_no

# 出网判定输入的字段白名单：住院号摘要 + 6 个医嘱字段，恰好 7 个键。
INPUT_FIELDS = (
    "inpatient_digest",
    "order_no",
    "content",
    "drug_name",
    "route",
    "frequency",
    "execute_dept_code",
)

SYSTEM_PROMPT = (
    "你是住院转科连带处置系统的医嘱语义判定助手。"
    "任务：判断「转科后这条长期医嘱在语义上是否仍属新病区」。"
    "结合医嘱内容、药名、给药途径、频次与执行科室读懂语义，给出归属结论；"
    "模型只判断归属是否变化以及是否需要停嘱草稿，不负责给出目标科室代码。"
    "输出约束：只输出一个 JSON 对象，不得输出 JSON 以外的任何文字；"
    "字段为 order_no(string)、judgement(string，取 KEEP|CHANGE_SITE|NEED_STOP_DRAFT|UNDETERMINED 四值之一，四值互斥)、"
    "confidence(0 到 1 的数字)、evidence_fields(字符串数组，必填)、"
    "evidence_snippet(string)、reasoning_summary(string)、"
    "stop_draft_content(string 或 null，可空)。"
    "证据不足或无法确定时 judgement 必须取 UNDETERMINED，不得猜测。"
    "不得输出姓名、身份证、联系方式等隐私字段。"
    "模型输出只是待人工复核的建议，不推进任何临床状态；停嘱草稿须经医师在 HIS 内复核处置。"
)


def _field(obj, name):
    """兼容 domain 对象与 dict 两种入参。"""
    if isinstance(obj, dict):
        return obj.get(name, "")
    return getattr(obj, name, "")


def build_judgement_input(order, transfer) -> dict:
    """构造脱敏后的判定输入：住院号摘要 + 医嘱白名单字段，仅此 7 个键。

    住院号原文不出网（只留 sha256 前 12 位摘要）；转科单的其它字段（含新病区
    代码）不进入判定输入，归属语义由模型读医嘱文本得出。
    """
    inpatient_no = _field(transfer, "inpatient_no")
    payload = {
        "inpatient_digest": digest_inpatient_no(inpatient_no),
        "order_no": str(_field(order, "order_no")),
        "content": str(_field(order, "content")),
        "drug_name": str(_field(order, "drug_name")),
        "route": str(_field(order, "route")),
        "frequency": str(_field(order, "frequency")),
        "execute_dept_code": str(_field(order, "execute_dept_code")),
    }
    assert_no_phi(payload)  # 出网前 PHI 断言（按契约调用 H 组 redact）
    return payload


def build_messages(order, transfer) -> list[dict]:
    """OpenAI-compatible messages：系统任务 + 单条脱敏 JSON 用户输入。

    返回值可直接放进 chat/completions 的 messages；user 内容是判定输入 JSON，
    可解析回 schema 所需的字段边界。
    """
    payload = build_judgement_input(order, transfer)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        },
    ]
