"""HIS 出站读 4 类资源：字段严格按 spec §2 解析，不以默认值兜底。

端点（统一 ``Authorization: Bearer $HIS_API_TOKEN``，由 HisClient 注入）：
  GET /api/v1/transfers/{transfer_id}
  GET /api/v1/transfers?ward=&date=
  GET /api/v1/inpatients/{inpatient_no}/orders?status=executing
  GET /api/v1/inpatients/{inpatient_no}/specimens?status=pending
  GET /api/v1/inpatients/{inpatient_no}/charges?category=bed

失败与空结果严格区分：任何 HTTP 错误 / 网络异常 / 字段缺失都抛异常（供上层
标记该资源节点失败并隔离），只有 HTTP 200 且列表为空才返回 ``[]``。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from itc.domain.models import (
    BedCharge,
    ExecutingOrder,
    FieldMissingError,
    Specimen,
    TransferOrder,
)

_ASCII_DIGITS = re.compile(r"^[0-9]+$")


def _validate_inpatient_no(inpatient_no) -> str:
    """住院号必须是非空纯 ASCII 数字且不含空格；否则拒绝发请求。"""
    if not isinstance(inpatient_no, str) or not _ASCII_DIGITS.match(inpatient_no):
        raise ValueError(
            "非法住院号：必须是非空纯数字字符串（不含空格 / 非数字字符 / 占位符）"
        )
    return inpatient_no


def _path(value) -> str:
    """URL 路径段编码防注入（纯数字住院号编码后不变）。"""
    return quote(str(value), safe="")


def _items(data) -> list:
    """解析列表型响应：兼容裸 list 与 {items: [...]} / {data: [...]} envelope。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    raise FieldMissingError("HIS 响应缺少列表字段（items）")


def _one(data) -> dict:
    """解析单对象型响应：兼容 {data: {...}} envelope。"""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data


class HisReader:
    def __init__(self, client):
        self.client = client

    def read_transfer(self, transfer_id) -> TransferOrder:
        resp = self.client.get(f"/api/v1/transfers/{_path(transfer_id)}")
        return TransferOrder.from_dict(_one(resp.json()))

    def read_today_transfers(self, ward, date) -> list:
        resp = self.client.get(
            "/api/v1/transfers", params={"ward": ward, "date": date}
        )
        return [TransferOrder.from_dict(x) for x in _items(resp.json())]

    def read_executing_orders(self, inpatient_no) -> list:
        no = _validate_inpatient_no(inpatient_no)
        resp = self.client.get(
            f"/api/v1/inpatients/{_path(no)}/orders",
            params={"status": "executing"},
        )
        return [ExecutingOrder.from_dict(x) for x in _items(resp.json())]

    def read_pending_specimens(self, inpatient_no) -> list:
        no = _validate_inpatient_no(inpatient_no)
        resp = self.client.get(
            f"/api/v1/inpatients/{_path(no)}/specimens",
            params={"status": "pending"},
        )
        raw = _items(resp.json())
        # 仅 status=pending 计入；非 pending 行不计入，也不参与字段严格校验。
        pending = [
            x for x in raw if isinstance(x, dict) and x.get("status") == "pending"
        ]
        return [Specimen.from_dict(x) for x in pending]

    def read_bed_charges(self, inpatient_no) -> list:
        no = _validate_inpatient_no(inpatient_no)
        resp = self.client.get(
            f"/api/v1/inpatients/{_path(no)}/charges",
            params={"category": "bed"},
        )
        raw = _items(resp.json())
        # 只计入 category=bed 的行；脏数据（金额空 / 负数 / 非法）标记需人工核对。
        bed_rows = [
            x for x in raw if isinstance(x, dict) and x.get("category") == "bed"
        ]
        charges = [BedCharge.from_dict(x) for x in bed_rows]
        for charge in charges:
            amount = str(charge.amount)
            reason = ""
            decimal_value = None
            if not amount.strip():
                reason = "金额为空，需人工核对"
            else:
                try:
                    decimal_value = Decimal(amount)
                    if decimal_value < 0:
                        reason = "金额为负数，需人工核对"
                except InvalidOperation:
                    reason = "金额无法转为 Decimal，需人工核对"
            # 契约允许以 needs_manual_check 属性标记脏数据行；脏数据不以 0 兜底。
            charge.needs_manual_check = bool(reason)
            charge.manual_check_reason = reason
            charge.amount_decimal = decimal_value
        return charges

    @staticmethod
    def bed_charge_amount(charge) -> Decimal:
        """床位费金额 Decimal 转换（不引入浮点误差）；非法值抛 ValueError。"""
        try:
            return Decimal(str(charge.amount))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"床位费金额非法：{charge.amount!r}") from exc

    @staticmethod
    def manual_check_charges(charges) -> list:
        """返回需人工核对的床位费行（金额空 / 负数 / 非法）。"""
        return [c for c in charges if getattr(c, "needs_manual_check", False)]

    @staticmethod
    def group_orders_by_dept(orders) -> dict:
        """医嘱按 execute_dept_code 分组、组内按 start_at 升序排序。"""
        groups: dict = {}
        for order in orders:
            groups.setdefault(order.execute_dept_code, []).append(order)
        for code in groups:
            groups[code].sort(key=lambda o: o.start_at)
        return {code: groups[code] for code in sorted(groups)}
