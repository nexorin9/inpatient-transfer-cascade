"""脱敏：住院号只留摘要；姓名 / 身份证 / 联系方式既不落盘也不出网。

出网前由 writer 调用 assert_no_phi 卡请求体字段边界；结构化日志落盘前经
redact_log 处理。字段名按「精确匹配」判定 PHI，避免误伤 execute_dept_name
这类业务字段（它们含 name 子串但不是姓名）。
"""
from __future__ import annotations

import hashlib

# 精确 PHI 字段名（小写）。业务字段 inpatient_no 不在此列：请求体允许带住院号，
# 日志里只做摘要化；execute_dept_name 等业务字段同样不在此列。
PHI_KEYS = frozenset({
    "name", "patient_name", "real_name", "full_name",
    "id_no", "id_card", "idcard", "id_number", "identity_no", "identity",
    "ssn", "social_id",
    "phone", "phone_no", "mobile", "tel", "telephone", "contact", "contact_no",
    "address", "home_address",
})

_INPATIENT_KEYS = frozenset({"inpatient_no", "inpatientno", "inpatient_id"})


def digest_inpatient_no(value) -> str:
    """住院号摘要：sha256 前 12 位（不可逆，不含原文）。"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _is_phi_key(key) -> bool:
    return str(key).strip().lower() in PHI_KEYS


def _is_inpatient_key(key) -> bool:
    return str(key).strip().lower() in _INPATIENT_KEYS


def redact_log(record: dict) -> dict:
    """把一条结构化日志记录脱敏后返回新 dict（不改原记录）。

    - PHI 字段名的值替换为 [REDACTED]（不落姓名 / 身份证 / 联系方式）
    - 住院号字段的值替换为 sha256 前 12 位摘要
    - 嵌套 dict / list 递归处理
    """
    out = {}
    for key, value in record.items():
        if _is_phi_key(key):
            out[key] = "[REDACTED]"
        elif _is_inpatient_key(key):
            out[key] = digest_inpatient_no(value)
        elif isinstance(value, dict):
            out[key] = redact_log(value)
        elif isinstance(value, list):
            out[key] = [redact_log(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def assert_no_phi(payload: dict) -> None:
    """出网前断言：请求体不含 name / id_no / phone / contact 等 PHI 字段。

    发现任一 PHI 字段（含嵌套 dict）即抛 ValueError，拒绝发请求。用精确字段名
    匹配，业务字段 execute_dept_name / inpatient_no 不受影响。
    """
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 dict，拒绝出网")

    def _walk(obj: dict) -> None:
        for key, value in obj.items():
            if _is_phi_key(key):
                raise ValueError(f"请求体含 PHI 字段，拒绝出网：{key}")
            if isinstance(value, dict):
                _walk(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _walk(item)

    _walk(payload)
