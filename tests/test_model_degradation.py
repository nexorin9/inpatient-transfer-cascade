"""M 组：不可达降级 / 零停嘱写 / 落库台账 / 恢复不回溯（任务 25）。

停嘱类写请求数用假 writer 计数；降级标记落库与台账可见用底座 db API 验证。
"""
from itc.domain.models import DispositionItem
from itc.model_gateway.degradation import (
    apply_degradation,
    recover_degradation,
    should_write_stop_draft,
)
from itc.store.db import fetch_dispositions, insert_disposition

REASON = "模型不可达（连接失败）"


def _raw_items():
    return [
        # 规则可判项：执行科室硬矛盾，照常可确认
        {
            "transfer_no": "T-1", "inpatient_no": "20260001",
            "kind": "order", "ref": "O-1",
            "judgement": "CHANGE_SITE", "state": "awaiting_confirm",
            "action": "CHANGE_SITE", "semantic": False, "confirmed_by": "",
            "manual": False, "degraded": False, "degraded_reason": "",
            "stop_draft_content": "",
        },
        # 语义项：长嘱归属判定
        {
            "transfer_no": "T-1", "inpatient_no": "20260001",
            "kind": "order", "ref": "O-2",
            "judgement": "KEEP", "state": "model_judged",
            "action": "KEEP", "semantic": True, "confirmed_by": "",
            "manual": False, "degraded": False, "degraded_reason": "",
            "stop_draft_content": "",
        },
        # 语义项：停嘱草稿（模型起草）
        {
            "transfer_no": "T-1", "inpatient_no": "20260001",
            "kind": "order", "ref": "O-3",
            "judgement": "NEED_STOP_DRAFT", "state": "awaiting_confirm",
            "action": "NEED_STOP_DRAFT", "semantic": True, "confirmed_by": "",
            "manual": False, "degraded": False, "degraded_reason": "",
            "stop_draft_content": "停药草稿内容",
        },
        # 已确认项（规则可判，降级前已确认）
        {
            "transfer_no": "T-1", "inpatient_no": "20260001",
            "kind": "specimen", "ref": "S-1",
            "judgement": "CHANGE_SITE", "state": "written_back",
            "action": "CHANGE_SITE", "semantic": False, "confirmed_by": "护士长",
            "manual": False, "degraded": False, "degraded_reason": "",
            "stop_draft_content": "",
        },
    ]


class FakeWriter:
    """只提供 DRAFT 级写接口，无 submit / sign 入口。"""

    def __init__(self):
        self.calls = {"create_stop_draft": 0, "write_execution_site": 0}

    def create_stop_draft(self, **kw):
        self.calls["create_stop_draft"] += 1
        return {"status": "DRAFT"}

    def write_execution_site(self, **kw):
        self.calls["write_execution_site"] += 1
        return {"ok": True}


def _dispatch(items, writer):
    for it in items:
        if should_write_stop_draft(it):
            writer.create_stop_draft(order_no=it["ref"])


def test_apply_degradation_keeps_rule_items_and_marks_semantic_manual():
    raw = _raw_items()
    out = apply_degradation(raw, REASON)
    # 规则可判项（含已确认项）原样保留，照常可确认
    assert out[0] == raw[0]
    assert out[3] == raw[3]
    # 语义项转人工待办
    for idx in (1, 2):
        assert out[idx]["judgement"] == "UNDETERMINED"
        assert out[idx]["manual"] is True
        assert out[idx]["degraded"] is True
        assert out[idx]["degraded_reason"] == REASON
        assert out[idx]["state"] != "awaiting_confirm"
    # 停嘱草稿被清空，不会被自动写成写请求
    assert out[2]["stop_draft_content"] == ""


def test_degraded_state_zero_stop_draft_writes():
    degraded = apply_degradation(_raw_items(), REASON)
    writer = FakeWriter()
    _dispatch(degraded, writer)
    assert writer.calls["create_stop_draft"] == 0
    # 对照：未降级时停嘱项可触发草稿写，证明假 writer 本身可用
    writer2 = FakeWriter()
    _dispatch(_raw_items(), writer2)
    assert writer2.calls["create_stop_draft"] == 1


def test_degraded_marks_persisted_and_visible_in_ledger(db_conn):
    degraded = apply_degradation(_raw_items(), REASON)
    for it in degraded:
        insert_disposition(db_conn, DispositionItem.from_dict(it))
    rows = fetch_dispositions(db_conn)
    marked = [r for r in rows if r["degraded"] == 1]
    assert len(marked) == 2
    assert all(r["degraded_reason"] == REASON for r in marked)
    # 规则可判项与已确认项不被误标降级
    assert sum(1 for r in rows if r["degraded"] == 0) == 2


def test_recover_rejudges_undetermined_but_never_touches_confirmed():
    degraded = apply_degradation(_raw_items(), REASON)
    recovered = recover_degradation(degraded)
    # 已确认项逐字段不回溯
    assert recovered[3] == degraded[3]
    # 规则可判项不回溯
    assert recovered[0] == degraded[0]
    # 未决语义项清降级标记，可重新判定
    for idx in (1, 2):
        assert recovered[idx]["degraded"] is False
        assert recovered[idx]["degraded_reason"] == ""
        assert recovered[idx].get("rejudge") is True
