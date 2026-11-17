"""执行科室硬矛盾判定（规则可判，模型不参与也成立，spec §5）。

执行科室 != 新病区且科室映射表无法解释 -> 判为硬矛盾，manual=True，需人工判定；
缺映射、或映射存在但语义不同（条目在却不指向新病区）同样无法解释，不猜。
每条判定返回可解释的 RuleHit（命中规则 id 与依据字段）。
"""
from __future__ import annotations

from itc.domain.models import RuleHit
from itc.rules.dept_map import entity_to_dict, iter_mappings, load_dept_map, resolve_dept

RULE_CONSISTENT = "EXEC_SITE_CONSISTENT"
RULE_EXPLAINED = "EXEC_SITE_MAP_EXPLAINED"
RULE_HARD_CONFLICT = "EXEC_SITE_HARD_CONFLICT"


def _equivalent_codes(entry):
    """取一个映射条目声明的等价科室编码集合。"""
    if not isinstance(entry, dict):
        return set()
    codes = {str(x) for x in (entry.get("equivalent_to") or []) if x is not None}
    maps_to = entry.get("maps_to")
    if isinstance(maps_to, str) and maps_to:
        codes.add(maps_to)
    return codes


def _map_explains(exec_code, to_code, dept_map) -> bool:
    """映射表能否解释执行科室 != 新病区这个差异。

    能解释的三种情形：执行科室条目把新病区列为等价；新病区条目把执行科室列为
    等价（对称）；两者归并到同一 canonical_code。缺映射（resolve_dept 为 None）
    一律视为无法解释，不猜。映射存在但语义不同即条目在、却既不等价也不归并，
    同样无法解释，判为硬矛盾。
    """
    mappings = iter_mappings(dept_map)
    exec_entry = mappings.get(exec_code)
    if exec_entry is None:
        return False
    if resolve_dept(exec_code, dept_map) is None:
        return False
    to_entry = mappings.get(to_code)
    if isinstance(exec_entry, dict) and to_code in _equivalent_codes(exec_entry):
        return True
    if isinstance(to_entry, dict) and exec_code in _equivalent_codes(to_entry):
        return True
    if isinstance(exec_entry, dict) and isinstance(to_entry, dict):
        a = exec_entry.get("canonical_code")
        b = to_entry.get("canonical_code")
        if a and b and str(a) == str(b):
            return True
    return False


def judge_execution_site(order, transfer, dept_map=None) -> RuleHit:
    """判定执行科室与新病区是否构成硬矛盾。

    order / transfer 可为 domain 对象或 dict；dept_map 为 None 时加载默认映射表。
    """
    o = entity_to_dict(order)
    t = entity_to_dict(transfer)
    exec_code = str(o.get("execute_dept_code") or "")
    to_code = str(t.get("to_dept_code") or "")
    fields = ["execute_dept_code", "to_dept_code"]
    dm = dept_map if dept_map is not None else load_dept_map()

    if exec_code == to_code:
        return RuleHit(
            rule_id=RULE_CONSISTENT,
            fields=fields,
            reason="执行科室与新病区一致，无需变更执行地点",
            manual=False,
        )
    if _map_explains(exec_code, to_code, dm):
        return RuleHit(
            rule_id=RULE_EXPLAINED,
            fields=fields + ["dept_map"],
            reason="科室映射表可解释执行科室与新病区的差异，可自动改执行地点",
            manual=False,
        )
    return RuleHit(
        rule_id=RULE_HARD_CONFLICT,
        fields=fields + ["dept_map"],
        reason="执行科室不等于新病区且科室映射表无法解释，判为硬矛盾，需人工判定，不自动处置",
        manual=True,
    )
