"""模型结构化判定输出的 JSON Schema 与强校验。

按 spec §2 与契约 §2 M：judgement 四值互斥、confidence 0-1、
evidence_fields 必填、stop_draft_content 可空。校验只认结构，不做业务兜底，
越界的取值一律拒绝，交由上层降级为「待人工判定」。
"""
from __future__ import annotations

import jsonschema

# judgement 四值互斥：用 enum 表达，取值不可能同时命中两个枚举项。
JUDGEMENTS = ("KEEP", "CHANGE_SITE", "NEED_STOP_DRAFT", "UNDETERMINED")

JUDGEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_no": {"type": "string"},
        "judgement": {"type": "string", "enum": list(JUDGEMENTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_fields": {"type": "array", "items": {"type": "string"}},
        "evidence_snippet": {"type": "string"},
        "reasoning_summary": {"type": "string"},
        # 可空：允许缺省、null 或空串
        "stop_draft_content": {"type": ["string", "null"]},
    },
    "required": [
        "order_no",
        "judgement",
        "confidence",
        "evidence_fields",
        "evidence_snippet",
        "reasoning_summary",
    ],
}


class JudgementSchemaError(ValueError):
    """模型输出不满足结构化 schema（缺字段 / 四值外取值 / confidence 越界等）。"""


def validate_judgement(payload) -> dict:
    """用 jsonschema 校验判定输出；通过返回 dict，失败抛 JudgementSchemaError。

    额外字段默认放行，便于上层附带 manual / degraded 等编排标记；required 列表
    保证核心字段一个都不能少。
    """
    try:
        jsonschema.validate(instance=payload, schema=JUDGEMENT_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise JudgementSchemaError(exc.message) from exc
    except jsonschema.SchemaError as exc:  # 常量 schema 不应出错，防御性兜底
        raise JudgementSchemaError(f"schema 非法：{exc.message}") from exc
    return dict(payload)
