"""临床实体与处置项：字段严格按 spec §2 解析，缺字段抛异常，不以默认值兜底。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 处置项 7 态（单向迁移表由 store/state_machine.py 定义）
STATES = (
    "pending_fetch",
    "scoped",
    "rule_judged",
    "model_judged",
    "awaiting_confirm",
    "written_back",
    "blocked",
    "failed",
)

JUDGEMENTS = ("KEEP", "CHANGE_SITE", "NEED_STOP_DRAFT", "UNDETERMINED")


class FieldMissingError(ValueError):
    """HIS 返回或入站转科单缺少规范要求的字段。"""


def _require(data: dict, keys: tuple[str, ...], where: str) -> None:
    missing = [k for k in keys if k not in data or data[k] is None]
    if missing:
        raise FieldMissingError(f"{where} 缺少必填字段：{', '.join(missing)}")


@dataclass
class TransferOrder:
    transfer_no: str
    inpatient_no: str
    from_dept_code: str
    to_dept_code: str
    from_dept_name: str
    to_dept_name: str
    ward_code: str
    transfer_at: str

    _KEYS = (
        "transfer_no", "inpatient_no", "from_dept_code", "to_dept_code",
        "from_dept_name", "to_dept_name", "ward_code", "transfer_at",
    )

    @classmethod
    def from_dict(cls, data: dict) -> "TransferOrder":
        _require(data, cls._KEYS, "转科单")
        for key in cls._KEYS:
            if not isinstance(data[key], str) or not data[key].strip():
                raise FieldMissingError(f"转科单字段 {key} 必须是非空字符串")
        return cls(**{k: data[k] for k in cls._KEYS})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecutingOrder:
    order_no: str
    content: str
    drug_name: str
    route: str
    frequency: str
    execute_dept_code: str
    start_at: str

    _KEYS = (
        "order_no", "content", "drug_name", "route",
        "frequency", "execute_dept_code", "start_at",
    )

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutingOrder":
        _require(data, cls._KEYS, "在执行医嘱")
        return cls(**{k: str(data[k]) for k in cls._KEYS})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Specimen:
    specimen_no: str
    test_name: str
    collect_dept_code: str
    status: str

    _KEYS = ("specimen_no", "test_name", "collect_dept_code", "status")

    @classmethod
    def from_dict(cls, data: dict) -> "Specimen":
        _require(data, cls._KEYS, "待送标本")
        return cls(**{k: str(data[k]) for k in cls._KEYS})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BedCharge:
    category: str
    fee_date: str
    amount: str
    dept_code: str

    _KEYS = ("category", "fee_date", "amount", "dept_code")

    @classmethod
    def from_dict(cls, data: dict) -> "BedCharge":
        _require(data, cls._KEYS, "床位费明细")
        return cls(**{k: str(data[k]) for k in cls._KEYS})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DispositionItem:
    """一条待处置项。写回前必须逐条确认；停嘱只到草稿。"""
    transfer_no: str
    inpatient_no: str
    kind: str  # order | specimen | charge
    ref: str  # order_no / specimen_no / charge 定位
    action: str = "UNDETERMINED"
    judgement: str = "UNDETERMINED"
    state: str = "pending_fetch"
    confidence: float = 0.0
    evidence_fields: list[str] = field(default_factory=list)
    evidence_snippet: str = ""
    reasoning_summary: str = ""
    stop_draft_content: str = ""
    execute_dept_code: str = ""
    execute_dept_name: str = ""
    effective_from: str = ""
    idempotency_key: str = ""
    rule_hits: list[str] = field(default_factory=list)
    manual_reason: str = ""
    degraded: bool = False
    degraded_reason: str = ""
    confirmed_by: str = ""
    role: str = ""
    run_id: str = ""
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DispositionItem":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class RuleHit:
    """可解释的规则命中输出。"""
    rule_id: str
    fields: list[str]
    reason: str
    manual: bool = False
    candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
