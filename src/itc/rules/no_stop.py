"""不可停品类守门（输血 / 危急值相关 / 抗菌药按院内目录，spec §5）。

命中药品名精确 / 类别前缀 / 院内编码任一 -> 不生成停嘱草稿、不进入可写清单，
标注为不可自动停嘱·需医师判断。目录缺失或损坏 -> 保守按不可停处理并提示
人工维护（catalog_error 标记），绝不默认放行。停嘱只到草稿，产品不代签。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from itc.domain.models import RuleHit
from itc.rules.dept_map import catalog_path, entity_to_dict

RULE_HIT = "NO_STOP_CATALOG_HIT"
RULE_ERROR = "NO_STOP_CATALOG_ERROR"
_DEFAULT_FILENAME = "no_stop_catalog.json"
_DEFAULT_LABEL = "不可自动停嘱·需医师判断"


class NoStopCatalogError(RuntimeError):
    """不可停品类目录缺失或损坏：守门必须保守处理，不能默认放行。"""


@dataclass
class NoStopRuleHit(RuleHit):
    """守门命中输出：额外带展示标签与目录错误标记。"""

    label: str = ""
    catalog_error: bool = False


def load_no_stop_catalog(path=None) -> dict:
    """读取院内不可停品类目录；缺失 / 损坏 / 结构非法一律抛 NoStopCatalogError。"""
    p = catalog_path(path, _DEFAULT_FILENAME)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise NoStopCatalogError(f"不可停品类目录读取失败：{exc}") from exc
    if not isinstance(data, dict):
        raise NoStopCatalogError("不可停品类目录顶层必须是 JSON 对象")
    return data


def _coerce_catalog(catalog):
    """catalog 可为已加载 dict、文件路径或 None（加载默认目录）。"""
    if isinstance(catalog, dict):
        return catalog
    return load_no_stop_catalog(catalog)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x) for x in value if x is not None]
    return [str(value)]


def _match(order, catalog):
    """返回（命中方式列表，依据字段列表）；未命中则两者皆空。"""
    o = entity_to_dict(order)
    hits, fields = [], []
    drug = str(o.get("drug_name") or "").strip()
    for name in _as_list(catalog.get("drug_names")):
        if drug and drug == name:
            hits.append(f"药品名精确命中（{name}）")
            if "drug_name" not in fields:
                fields.append("drug_name")
            break
    cat_fields = _as_list(catalog.get("category_fields")) or ["content", "drug_name"]
    prefixes = _as_list(catalog.get("category_prefixes"))
    for fname in cat_fields:
        value = str(o.get(fname) or "").strip()
        if not value:
            continue
        for prefix in prefixes:
            if prefix and value.startswith(prefix):
                hits.append(f"类别前缀命中（{prefix}，字段 {fname}）")
                if fname not in fields:
                    fields.append(fname)
                break
    code_fields = _as_list(catalog.get("code_fields")) or ["hospital_code"]
    codes = set(_as_list(catalog.get("hospital_codes")))
    for fname in code_fields:
        value = str(o.get(fname) or "").strip()
        if value and value in codes:
            hits.append(f"院内编码命中（{value}，字段 {fname}）")
            if fname not in fields:
                fields.append(fname)
    return hits, fields


def is_no_stop(order, catalog=None):
    """守门判定：命中返回 NoStopRuleHit，未命中返回 None。

    catalog 可传 dict、文件路径（str/Path）或 None；目录缺失 / 损坏时不把异常
    抛给调用方，而是返回带 catalog_error 的保守命中，确保任何情况下都不放行。
    """
    try:
        cat = _coerce_catalog(catalog)
    except NoStopCatalogError as exc:
        return NoStopRuleHit(
            rule_id=RULE_ERROR,
            fields=["no_stop_catalog"],
            reason=(
                "不可停品类目录缺失或损坏，保守按不可停处理：不生成停嘱草稿、不进入可写清单；"
                f"请信息科人工维护目录（catalog_error：{exc}）"
            ),
            manual=True,
            candidates=["catalog_error"],
            label=_DEFAULT_LABEL,
            catalog_error=True,
        )
    hits, fields = _match(order, cat)
    if not hits:
        return None
    label = str(cat.get("label") or _DEFAULT_LABEL)
    reason = (
        "命中不可停品类（" + "；".join(hits) + "），不生成停嘱草稿、不进入可写清单，"
        + label + "，需医师在 HIS 内自行处置"
    )
    return NoStopRuleHit(
        rule_id=RULE_HIT,
        fields=fields or ["drug_name"],
        reason=reason,
        manual=True,
        candidates=[],
        label=label,
        catalog_error=False,
    )
