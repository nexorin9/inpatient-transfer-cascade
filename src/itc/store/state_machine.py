"""处置项状态机：单向前进的 7 态迁移、非法迁移拒绝且状态不变、节点终态映射。

状态持久化在 SQLite（底座 db.py），本模块只做迁移校验与状态推进；
重跑另开记录，绝不回退既有记录。
"""
from __future__ import annotations

import json

from itc.domain.models import STATES, DispositionItem
from itc.store.db import append_audit, insert_disposition, update_disposition
from itc.store.guards import guard_write


class IllegalTransition(ValueError):
    """非法状态迁移：抛出时原状态保持不变。"""


# 7 态单向迁移表（happy path 严格按契约流程）：
# pending_fetch -> scoped -> rule_judged -> model_judged -> awaiting_confirm
# awaiting_confirm -> written_back / blocked / failed；终态无出边。
TRANSITIONS: dict[str, frozenset[str]] = {
    "pending_fetch": frozenset({"scoped"}),
    "scoped": frozenset({"rule_judged"}),
    "rule_judged": frozenset({"model_judged"}),
    "model_judged": frozenset({"awaiting_confirm"}),
    "awaiting_confirm": frozenset({"written_back", "blocked", "failed"}),
    "written_back": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
}

TERMINAL_STATES = frozenset({"written_back", "blocked", "failed"})

# 节点终态 -> 处置项状态。
NODE_TERMINAL_TO_STATE = {
    "success": "written_back",
    "failed": "failed",
    "blocked": "blocked",
}

# 序号用于保证「只允许前进」：目标状态序号必须严格大于来源状态。
_STATE_RANK = {
    "pending_fetch": 0,
    "scoped": 1,
    "rule_judged": 2,
    "model_judged": 3,
    "awaiting_confirm": 4,
    "written_back": 5,
    "blocked": 5,
    "failed": 5,
}

# 阻断 / 失败是中止类终态：任何非终态都可直接落到它们（取数失败、规则拒绝自动处置）。
# 这仍属于前进（终态序号 5 大于所有非终态），且终态本身不接受任何出边。
ABORT_STATES = frozenset({"blocked", "failed"})


def can_transition(frm: str, to: str) -> bool:
    """是否允许从 frm 迁移到 to；只允许前进，终态不可再变。"""
    if frm not in STATES or to not in STATES:
        return False
    if frm in TERMINAL_STATES:
        return False
    if _STATE_RANK[to] <= _STATE_RANK[frm]:
        return False
    if to in ABORT_STATES:
        return True
    return to in TRANSITIONS.get(frm, frozenset())


def assert_role(role: str | None, action: str) -> bool:
    """状态推进时的角色校验，委托给 guards.guard_write。"""
    return guard_write(role, action)


def _fetch_item(conn, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM dispositions WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row is not None else None


def transition(conn, item_id: int, to_state: str, *, role: str | None = None) -> dict:
    """推进处置项状态。

    非法迁移抛 IllegalTransition，且在抛错前不写库，保证状态不变。
    回写到 written_back 属于敏感动作：显式传入非授权角色时拒绝。
    """
    row = _fetch_item(conn, item_id)
    if row is None:
        raise IllegalTransition(f"处置项 {item_id} 不存在")
    frm = row["state"]
    if to_state not in STATES:
        raise IllegalTransition(f"未知目标状态：{to_state}")
    if not can_transition(frm, to_state):
        raise IllegalTransition(f"非法迁移 {frm} -> {to_state}（状态保持 {frm}）")
    if to_state == "written_back":
        assert_role(role, "write_back")
    update_disposition(conn, item_id, state=to_state)
    append_audit(
        conn,
        run_id=row.get("run_id") or "",
        node_id=str(item_id),
        event_type="state_transition",
        node_type="disposition",
        actor=role or "",
        payload={
            "from": frm,
            "to": to_state,
            "transfer_no": row["transfer_no"],
            "order_no": row.get("order_no") or "",
            "execute_dept_code": row.get("execute_dept_code") or "",
        },
    )
    return _fetch_item(conn, item_id)


def apply_node_terminal(conn, item_id: int, node_status: str,
                        *, role: str | None = None, to_state: str | None = None) -> dict:
    """把图节点终态（success / failed / blocked）落到处置项状态。"""
    target = to_state or NODE_TERMINAL_TO_STATE.get(node_status)
    if target is None:
        raise IllegalTransition(f"未知节点终态：{node_status}")
    return transition(conn, item_id, target, role=role)


def start_rerun(conn, item_id: int, run_id: str) -> int:
    """重跑另开记录：新建一条 pending_fetch 处置项，不回退 / 不修改原记录。

    幂等键按业务字段保留（不含 run_id 与时间戳），因此重跑不会触发第二次写请求。
    """
    row = _fetch_item(conn, item_id)
    if row is None:
        raise IllegalTransition(f"处置项 {item_id} 不存在")
    ref = row.get("order_no") or row.get("specimen_no") or row.get("charge_ref") or ""
    item = DispositionItem(
        transfer_no=row["transfer_no"],
        inpatient_no=row["inpatient_no"],
        kind=row["kind"],
        ref=ref,
        action=row.get("action") or "",
        judgement=row.get("judgement") or "UNDETERMINED",
        state="pending_fetch",
        confidence=row.get("confidence") or 0.0,
        evidence_fields=_json_list(row.get("evidence_fields")),
        evidence_snippet=row.get("evidence_snippet") or "",
        reasoning_summary=row.get("reasoning_summary") or "",
        stop_draft_content=row.get("stop_draft_content") or "",
        execute_dept_code=row.get("execute_dept_code") or "",
        execute_dept_name=row.get("execute_dept_name") or "",
        effective_from=row.get("effective_from") or "",
        idempotency_key=row.get("idempotency_key") or "",
        rule_hits=_json_list(row.get("rule_hits")),
        manual_reason=row.get("manual_reason") or "",
        degraded=bool(row.get("degraded")),
        degraded_reason=row.get("degraded_reason") or "",
        confirmed_by="",
        role="",
        run_id=run_id,
    )
    return insert_disposition(conn, item)


def _json_list(value) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
