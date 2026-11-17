"""非法迁移矩阵 + 越权写入护栏：所有「必须不发生」都变成可复跑断言。"""
from __future__ import annotations

import itertools

import pytest

from itc.domain.models import STATES, DispositionItem
from itc.store import guards, state_machine as sm
from itc.store.db import insert_disposition

NON_TERMINAL = [s for s in STATES if s not in sm.TERMINAL_STATES]
# 「未确认」= 尚未进入 awaiting_confirm 的前置态；awaiting_confirm 本身已逐条确认。
UNCONFIRMED = [s for s in NON_TERMINAL if s != "awaiting_confirm"]
RANK = {
    "pending_fetch": 0, "scoped": 1, "rule_judged": 2, "model_judged": 3,
    "awaiting_confirm": 4, "written_back": 5, "blocked": 5, "failed": 5,
}


def _insert(conn, state: str) -> int:
    return insert_disposition(conn, DispositionItem(
        transfer_no="T-MATRIX", inpatient_no="123456", kind="order", ref="ORD-M",
        state=state,
    ))


def _state(conn, item_id: int) -> str:
    return conn.execute(
        "SELECT state FROM dispositions WHERE id=?", (item_id,)
    ).fetchone()["state"]


def test_unconfirmed_to_written_back_matrix(db_conn):
    """未确认（任何前置态）直接到已回写，一律拒绝且状态不变。"""
    for frm in UNCONFIRMED:
        assert sm.can_transition(frm, "written_back") is False
        item_id = _insert(db_conn, frm)
        with pytest.raises(sm.IllegalTransition):
            sm.transition(db_conn, item_id, "written_back", role="护士长")
        assert _state(db_conn, item_id) == frm


def test_written_back_to_written_back_matrix(db_conn):
    """已回写再改写 / 再回写：终态无出边，全部拒绝。"""
    for target in STATES:
        assert sm.can_transition("written_back", target) is False
        item_id = _insert(db_conn, "written_back")
        with pytest.raises(sm.IllegalTransition):
            sm.transition(db_conn, item_id, target, role="护士长")
        assert _state(db_conn, item_id) == "written_back"


def test_failed_and_blocked_to_written_back_matrix(db_conn):
    for frm in ("failed", "blocked"):
        for target in ("written_back", "awaiting_confirm", "scoped", "pending_fetch"):
            assert sm.can_transition(frm, target) is False
            item_id = _insert(db_conn, frm)
            with pytest.raises(sm.IllegalTransition):
                sm.transition(db_conn, item_id, target)
            assert _state(db_conn, item_id) == frm


def test_backward_transition_matrix():
    """状态只允许前进：任何目标序号不大于来源序号的迁移都被拒。"""
    for frm, to in itertools.product(STATES, STATES):
        if RANK[to] <= RANK[frm]:
            assert sm.can_transition(frm, to) is False
    assert sm.can_transition("pending_fetch", "scoped") is True
    assert sm.can_transition("awaiting_confirm", "written_back") is True


def test_every_illegal_pair_leaves_state_unchanged(db_conn):
    """全矩阵：所有 can_transition=False 的组合都抛错且状态不变。"""
    for frm, to in itertools.product(STATES, STATES):
        if sm.can_transition(frm, to):
            continue
        item_id = _insert(db_conn, frm)
        with pytest.raises(sm.IllegalTransition):
            sm.transition(db_conn, item_id, to, role="护士长")
        assert _state(db_conn, item_id) == frm
    for bad in ("unknown", "", "WRITTEN_BACK"):
        item_id = _insert(db_conn, "scoped")
        with pytest.raises(sm.IllegalTransition):
            sm.transition(db_conn, item_id, bad)
        assert _state(db_conn, item_id) == "scoped"


def test_zero_successful_illegal_transitions(db_conn):
    successes = 0
    for frm, to in itertools.product(STATES, STATES):
        if sm.can_transition(frm, to):
            continue
        item_id = _insert(db_conn, frm)
        try:
            sm.transition(db_conn, item_id, to)
        except sm.IllegalTransition:
            continue
        successes += 1
    assert successes == 0


def test_role_gate_on_writeback(db_conn):
    """越权写入防护：护士长可回写，医师 / 经办不可回写。"""
    item_id = _insert(db_conn, "awaiting_confirm")
    with pytest.raises(guards.WriteGuardError):
        sm.transition(db_conn, item_id, "written_back", role="医师")
    assert _state(db_conn, item_id) == "awaiting_confirm"
    with pytest.raises(guards.WriteGuardError):
        sm.transition(db_conn, item_id, "written_back", role="经办")
    assert _state(db_conn, item_id) == "awaiting_confirm"
    assert sm.transition(db_conn, item_id, "written_back", role="护士长")["state"] == "written_back"


def test_assert_role_rejects_unauthorized(db_conn):
    assert sm.assert_role("护士长", "confirm") is True
    assert sm.assert_role("护士长", "write_back") is True
    with pytest.raises(guards.WriteGuardError):
        sm.assert_role("医师", "write_back")
    with pytest.raises(guards.WriteGuardError):
        sm.assert_role("护士长", "submit_order")
