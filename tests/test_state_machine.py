"""处置项状态机：7 态单向迁移、非法迁移状态不变、节点终态映射、重跑另开记录。"""
from __future__ import annotations

import pytest

from itc.domain.models import STATES, DispositionItem
from itc.store import state_machine as sm
from itc.store.db import insert_disposition


def _insert(conn, **kwargs) -> int:
    base = dict(transfer_no="T-001", inpatient_no="880011", kind="order", ref="ORD-1")
    base.update(kwargs)
    return insert_disposition(conn, DispositionItem(**base))


def _state(conn, item_id: int) -> str:
    return conn.execute(
        "SELECT state FROM dispositions WHERE id=?", (item_id,)
    ).fetchone()["state"]


def _audit_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"])


def test_states_come_from_domain_and_transitions_cover_all():
    assert set(sm.STATES) == set(STATES)
    assert set(sm.TRANSITIONS) == set(STATES)
    assert sm.TRANSITIONS["pending_fetch"] == frozenset({"scoped"})
    assert sm.TRANSITIONS["awaiting_confirm"] == frozenset({"written_back", "blocked", "failed"})


def test_happy_path_advances_one_step_at_a_time(db_conn):
    item_id = _insert(db_conn)
    assert _state(db_conn, item_id) == "pending_fetch"
    for nxt in ("scoped", "rule_judged", "model_judged", "awaiting_confirm", "written_back"):
        result = sm.transition(db_conn, item_id, nxt, role="护士长")
        assert result["state"] == nxt
        assert _state(db_conn, item_id) == nxt


def test_illegal_transition_raises_and_state_unchanged(db_conn):
    item_id = _insert(db_conn)
    before = _state(db_conn, item_id)
    n_before = _audit_count(db_conn)
    with pytest.raises(sm.IllegalTransition) as exc:
        sm.transition(db_conn, item_id, "written_back")
    assert "非法迁移" in str(exc.value)
    assert _state(db_conn, item_id) == before
    assert _audit_count(db_conn) == n_before  # 失败不落审计事件


def test_unknown_state_rejected(db_conn):
    item_id = _insert(db_conn)
    with pytest.raises(sm.IllegalTransition):
        sm.transition(db_conn, item_id, "not_a_state")
    assert _state(db_conn, item_id) == "pending_fetch"


def test_missing_item_rejected(db_conn):
    with pytest.raises(sm.IllegalTransition):
        sm.transition(db_conn, 999999, "scoped")


def test_terminal_states_have_no_outgoing_edges():
    for terminal in sm.TERMINAL_STATES:
        assert sm.TRANSITIONS[terminal] == frozenset()
        for target in STATES:
            assert sm.can_transition(terminal, target) is False


def test_forward_only_no_backward_transition(db_conn):
    assert sm.can_transition("scoped", "pending_fetch") is False
    assert sm.can_transition("awaiting_confirm", "rule_judged") is False
    assert sm.can_transition("written_back", "awaiting_confirm") is False
    item_id = _insert(db_conn)
    sm.transition(db_conn, item_id, "scoped")
    with pytest.raises(sm.IllegalTransition):
        sm.transition(db_conn, item_id, "pending_fetch")
    assert _state(db_conn, item_id) == "scoped"


def test_abort_terminal_states_are_forward_only(db_conn):
    # 取数失败 / 规则拒绝自动处置可以提前落到终态，但终态本身不再变化。
    assert sm.can_transition("pending_fetch", "failed") is True
    assert sm.can_transition("scoped", "blocked") is True
    item_id = _insert(db_conn)
    assert sm.transition(db_conn, item_id, "failed")["state"] == "failed"
    with pytest.raises(sm.IllegalTransition):
        sm.transition(db_conn, item_id, "written_back")
    assert _state(db_conn, item_id) == "failed"


def test_node_terminal_maps_to_disposition_state(db_conn):
    assert sm.NODE_TERMINAL_TO_STATE == {
        "success": "written_back", "failed": "failed", "blocked": "blocked",
    }
    for node_status, expected in sm.NODE_TERMINAL_TO_STATE.items():
        item_id = _insert(db_conn, transfer_no=f"T-{node_status}")
        if expected == "written_back":
            for s in ("scoped", "rule_judged", "model_judged", "awaiting_confirm"):
                sm.transition(db_conn, item_id, s)
            result = sm.apply_node_terminal(db_conn, item_id, node_status, role="护士长")
        else:
            result = sm.apply_node_terminal(db_conn, item_id, node_status)
        assert result["state"] == expected
        assert _state(db_conn, item_id) == expected
    with pytest.raises(sm.IllegalTransition):
        sm.apply_node_terminal(db_conn, _insert(db_conn), "unknown")


def test_successful_transition_appends_audit_event(db_conn):
    item_id = _insert(db_conn)
    sm.transition(db_conn, item_id, "scoped")
    rows = [dict(r) for r in db_conn.execute(
        "SELECT * FROM audit_events ORDER BY seq ASC"
    ).fetchall()]
    assert rows[-1]["event_type"] == "state_transition"
    assert rows[-1]["node_id"] == str(item_id)
    assert '"to": "scoped"' in rows[-1]["payload"]


def test_rerun_opens_new_record_without_rewinding(db_conn):
    item_id = _insert(db_conn, idempotency_key="IDEM-FIXED", run_id="RUN-1")
    sm.transition(db_conn, item_id, "scoped")
    original_state = _state(db_conn, item_id)
    new_id = sm.start_rerun(db_conn, item_id, "RUN-2")
    assert new_id != item_id
    assert _state(db_conn, item_id) == original_state  # 原记录不回退、不改写
    assert _state(db_conn, new_id) == "pending_fetch"
    new_row = dict(db_conn.execute(
        "SELECT * FROM dispositions WHERE id=?", (new_id,)
    ).fetchone())
    assert new_row["run_id"] == "RUN-2"
    assert new_row["idempotency_key"] == "IDEM-FIXED"  # 业务字段不变，键不变
    assert new_row["transfer_no"] == "T-001"
    count = int(db_conn.execute("SELECT COUNT(*) AS n FROM dispositions").fetchone()["n"])
    assert count == 2
