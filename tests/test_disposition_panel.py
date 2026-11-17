"""任务 42：逐条处置清单、逐条确认回写、批量独立、失败重试。"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from itc.ui import states
from itc.ui.disposition_panel import ACTION_COL, COLUMNS, STATUS_COL, DispositionPanel


def make_items(count: int = 3) -> list[dict]:
    return [
        {
            "transfer_no": "T-1", "inpatient_no": "001", "kind": "order",
            "ref": f"O{i}", "judgement": "CHANGE_SITE", "state": "awaiting_confirm",
            "confidence": 0.8 + i * 0.05,
            "evidence_fields": ["execute_dept_code", "to_dept_code"],
            "evidence_snippet": f"第 {i} 条执行科室与新病区不一致",
            "execute_dept_name": "检验科", "execute_dept_code": "LAB",
            "idempotency_key": f"key-{i}",
        }
        for i in range(count)
    ]


def test_columns_cover_required_business_fields(qapp):
    for label in ("业务对象", "判定", "证据字段", "证据片段", "置信度", "状态"):
        assert label in COLUMNS
    panel = DispositionPanel(writer=lambda d: {"ok": True})
    panel.set_items(make_items(2))
    model = panel.table_view.model()
    assert model.rowCount() == 2
    assert model.columnCount() == len(COLUMNS)
    assert model.headerData(COLUMNS.index("业务对象"), 1) == "业务对象"


def test_display_values_are_real(qapp):
    panel = DispositionPanel(writer=lambda d: {"ok": True})
    panel.set_items(make_items(1))
    model = panel.table_view.model()
    assert "O0" in model.index(0, COLUMNS.index("业务对象")).data()
    assert "0.80" == model.index(0, COLUMNS.index("置信度")).data()
    assert "execute_dept_code" in model.index(0, COLUMNS.index("证据字段")).data()
    assert "第 0 条" in model.index(0, COLUMNS.index("证据片段")).data()
    assert panel.status_text_for(0) == "待确认"


def test_unconfirmed_rows_issue_zero_write_requests(qapp):
    calls: list[dict] = []
    panel = DispositionPanel(writer=lambda d: calls.append(d) or {"ok": True})
    panel.set_items(make_items(4))
    assert calls == []
    assert panel.write_request_count() == 0
    assert panel.confirmed_items() == []
    # 装载数据本身绝不触发写请求


def test_confirm_each_row_issues_exactly_one_write_request(qapp):
    calls: list[dict] = []
    panel = DispositionPanel(writer=lambda d: calls.append(d) or {"ok": True})
    panel.set_items(make_items(3))
    assert panel.confirm_row(0) is True
    assert panel.confirm_row(1) is True
    assert len(calls) == 2
    assert panel.write_request_count() == 2
    assert [c["ref"] for c in calls] == ["O0", "O1"]
    assert [it["state"] for it in panel.confirmed_items()] == ["written_back", "written_back"]
    assert panel.status_text_for(0) == "已回写"


def test_confirm_all_each_row_once(qapp):
    calls: list[dict] = []
    panel = DispositionPanel(writer=lambda d: calls.append(d) or {"ok": True})
    panel.set_items(make_items(4))
    result = panel.confirm_all()
    assert result == {"attempted": 4, "succeeded": 4, "failed": []}
    assert len(calls) == 4
    assert sorted(c["ref"] for c in calls) == ["O0", "O1", "O2", "O3"]
    assert len(panel.confirmed_items()) == 4


def test_writer_none_only_changes_local_state(qapp):
    panel = DispositionPanel()  # 未注入 writer
    panel.set_items(make_items(2))
    assert panel.confirm_row(0) is True
    assert panel.write_request_count() == 0
    assert panel.confirmed_items()[0]["state"] == "written_back"
    assert panel.confirmed_items()[0]["confirmed_by"] == "护士长"


def test_single_failure_does_not_affect_other_rows(qapp):
    calls: list[dict] = []

    def flaky(payload: dict) -> dict:
        calls.append(payload)
        if payload["ref"] == "O2":
            raise RuntimeError("HIS 500")
        return {"ok": True}

    panel = DispositionPanel(writer=flaky)
    panel.set_items(make_items(5))
    result = panel.confirm_all()
    assert result["attempted"] == 5
    assert result["succeeded"] == 4
    assert result["failed"] == [2]
    assert len(calls) == 5  # 失败项也只尝试一次
    states_by_ref = {it["ref"]: it["state"] for it in panel.confirmed_items()}
    assert all(v == "written_back" for v in states_by_ref.values())
    assert "O2" not in states_by_ref
    assert panel.failed_rows() == [2]


def test_failed_row_has_retry_entry_and_status_hint(qapp):
    panel = DispositionPanel(writer=lambda d: {"ok": False, "reason": "写回超时"})
    panel.set_items(make_items(2))
    assert panel.confirm_row(1) is False
    assert panel.status_text_for(1) == "失败·待重写"
    entries = panel.pending_writebacks()
    assert len(entries) == 1
    assert entries[0]["row"] == 1
    assert entries[0]["idempotency_key"] == "key-1"
    assert entries[0]["attempts"] == 1
    assert "写回超时" in entries[0]["reason"]
    label = panel.table_view.indexWidget(
        panel.table_view.model().index(1, STATUS_COL))
    assert isinstance(label, QLabel) and "待重写" in label.text()
    button = panel.table_view.indexWidget(
        panel.table_view.model().index(1, ACTION_COL))
    assert isinstance(button, QPushButton) and button.text() == "重试"


def test_retry_failed_reuses_same_idempotency_key(qapp):
    calls: list[dict] = []
    panel = DispositionPanel(writer=lambda d: calls.append(d) or {"ok": False})
    panel.set_items(make_items(2))
    panel.confirm_row(0)
    panel.set_writer(lambda d: calls.append(d) or {"ok": True})
    assert panel.retry_failed(0) is True
    assert panel.status_text_for(0) == "已回写"
    assert panel.pending_writebacks() == []
    assert [c["idempotency_key"] for c in calls] == ["key-0", "key-0"]
    assert panel.retry_failed(1) is False  # 非失败行不可重试


def test_status_labels_are_widgets_with_semantic_style(qapp):
    panel = DispositionPanel(writer=lambda d: {"ok": True})
    panel.set_items(make_items(1))
    label = panel.table_view.indexWidget(
        panel.table_view.model().index(0, STATUS_COL))
    assert isinstance(label, QLabel)
    assert label.text() == "待确认"
    assert "#FEF3C7" in label.styleSheet()  # 琥珀底
    panel.confirm_row(0)
    assert "#DCFCE7" in label.styleSheet()  # 已回写绿底


def test_filter_and_clear_with_second_confirmation(qapp):
    panel = DispositionPanel(writer=lambda d: {"ok": True})
    panel.set_items(make_items(3))
    panel.confirm_row(0)
    assert panel.set_filter("已回写") == [0]
    assert panel.set_filter("待确认") == [1, 2]
    assert panel.set_filter("全部") == [0, 1, 2]
    with states.auto_answer(False):
        assert panel.clear_all() is False
    assert panel.table_view.model().rowCount() == 3
    with states.auto_answer(True):
        assert panel.clear_all() is True
    assert panel.table_view.model().rowCount() == 0
    assert panel.empty_placeholder.state() == "empty"
