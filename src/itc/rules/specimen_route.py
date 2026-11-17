"""标本去向唯一性判定（实体绑定不唯一即拒绝自动处置，spec §5）。

候选送检科室 >1（或无法定位）-> manual=True，产出人工待办，拒绝自动处置，
写请求数为 0，处置状态停在 awaiting_confirm 之前。依据科室映射表与标本去向
目录，不按最近似猜。每条判定返回可解释的 RuleHit。
"""
from __future__ import annotations

import json

from itc.domain.models import RuleHit
from itc.rules.dept_map import catalog_path, entity_to_dict, resolve_dept

RULE_UNIQUE = "SPECIMEN_ROUTE_UNIQUE"
RULE_AMBIGUOUS = "SPECIMEN_ROUTE_AMBIGUOUS"
_DEFAULT_FILENAME = "specimen_routes.json"


def load_specimen_routes(path=None) -> dict:
    """读取标本送检去向目录；缺失或损坏返回空目录（候选为空 -> 人工判定）。"""
    p = catalog_path(path, _DEFAULT_FILENAME)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dedup(items):
    """候选科室去重且保序，便于人工待办稳定展示。"""
    seen, out = set(), []
    for item in items:
        if item is None:
            continue
        s = str(item)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _resolve_candidates(specimen, candidates):
    """确定候选送检科室：显式入参优先，否则按目录的项目名 / 采集科室查。"""
    if candidates is not None:
        seq = candidates if isinstance(candidates, (list, tuple, set)) else [candidates]
        return _dedup(seq)
    s = entity_to_dict(specimen)
    cat = load_specimen_routes()
    by_name = cat.get("by_test_name") if isinstance(cat.get("by_test_name"), dict) else {}
    by_dept = cat.get("by_collect_dept") if isinstance(cat.get("by_collect_dept"), dict) else {}
    test_name = str(s.get("test_name") or "")
    collect_dept = str(s.get("collect_dept_code") or "")
    raw = by_name.get(test_name)
    if raw is None:
        raw = by_dept.get(collect_dept)
    if raw is None:
        raw = cat.get("default_candidates")
    if raw is None:
        raw = []
    return _dedup(raw if isinstance(raw, (list, tuple, set)) else [raw])


def _describe(cands, dept_map):
    """候选科室代码附上可解析的科室名，便于人工待办阅读；解析不出就保留原值。"""
    parts = []
    for code in cands:
        name = resolve_dept(code, dept_map)
        parts.append(f"{code}（{name}）" if name and name != code else code)
    return parts


def judge_specimen_route(specimen, transfer, dept_map=None, candidates=None) -> RuleHit:
    """判定标本送检去向是否唯一。

    candidates 显式给出时优先使用；否则按 specimen_routes.json 查候选。
    dept_map 仅用于把候选科室代码解析成可读名称，不参与唯一性判定。
    """
    s = entity_to_dict(specimen)
    t = entity_to_dict(transfer)
    to_code = str(t.get("to_dept_code") or "")
    ref = str(s.get("specimen_no") or "")
    cands = _resolve_candidates(specimen, candidates)
    fields = ["specimen_no", "test_name", "collect_dept_code", "to_dept_code", "candidates"]

    if len(cands) == 1:
        shown = _describe(cands, dept_map)[0]
        return RuleHit(
            rule_id=RULE_UNIQUE,
            fields=fields,
            reason=f"标本 {ref} 候选送检科室唯一（{shown}），新病区 {to_code}，送检去向可自动处置",
            manual=False,
            candidates=list(cands),
        )
    shown = _describe(cands, dept_map)
    if len(cands) > 1:
        reason = (
            f"标本 {ref} 候选送检科室大于一个（{'、'.join(shown)}），去向无法唯一确定，"
            "拒绝自动处置，需人工判断"
        )
    else:
        reason = (
            f"标本 {ref} 无法定位候选送检科室（标本去向目录缺项），拒绝自动处置，需人工判断"
        )
    return RuleHit(
        rule_id=RULE_AMBIGUOUS,
        fields=fields,
        reason=reason,
        manual=True,
        candidates=list(cands),
    )


def build_manual_todo(specimen, hit):
    """把歧义判定转成人工待办项：含候选科室与原因，状态停在 awaiting_confirm 之前。"""
    s = entity_to_dict(specimen)
    return {
        "kind": "specimen",
        "ref": str(s.get("specimen_no") or ""),
        "manual": True,
        "state": "rule_judged",
        "label": "待人工处置·标本去向歧义",
        "rule_id": hit.rule_id,
        "candidates": list(hit.candidates),
        "reason": hit.reason,
    }
