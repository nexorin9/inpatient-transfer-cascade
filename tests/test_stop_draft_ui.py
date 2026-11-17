"""任务 43：停嘱草稿「需医师复核」标注、无提交护栏、不可停品类。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel, QPushButton

from itc.store import guards
from itc.ui import tokens
from itc.ui.disposition_panel import ACTION_COL, STATUS_COL, DispositionPanel
from itc.ui.main_window import MainWindow
from itc.ui.tray import MENU_ITEMS, TrayController

SRC_UI = Path(__file__).resolve().parents[1] / "src" / "itc" / "ui"

STOP_ITEM = {
    "transfer_no": "T-1", "inpatient_no": "001", "kind": "order", "ref": "O-STOP",
    "judgement": "NEED_STOP_DRAFT", "state": "awaiting_confirm", "confidence": 0.72,
    "stop_draft_content": "停用 0.9% 氯化钠注射液 250ml qd，需医师复核",
    "reasoning_summary": "模型建议由医师复核停嘱",
    "evidence_fields": ["order_no", "content", "execute_dept_code"],
    "evidence_snippet": "长期医嘱执行科室与新病区不一致",
}

NO_STOP_ITEM = {
    "transfer_no": "T-1", "inpatient_no": "001", "kind": "order", "ref": "O-NOSTOP",
    "judgement": "UNDETERMINED", "state": "awaiting_confirm", "confidence": 0.0,
    "rule_hits": ["no_stop"], "manual_reason": "不可停品类命中",
    "stop_draft_content": "本行不应产出草稿",
}


def test_stop_draft_row_label_and_disabled_button(qapp):
    calls = []
    panel = DispositionPanel(writer=lambda d: calls.append(d) or {"ok": True})
    panel.set_items([STOP_ITEM, NO_STOP_ITEM])
    assert panel.stop_draft_rows() == [0]
    assert panel.status_text_for(0) == tokens.STOP_DRAFT_LABEL == "草稿·需医师复核"
    label = panel.table_view.indexWidget(
        panel.table_view.model().index(0, STATUS_COL))
    assert isinstance(label, QLabel)
    assert label.text() == "草稿·需医师复核"
    assert tokens.COLORS["neutral_header"] in label.styleSheet()  # 灰底深字
    button = panel.table_view.indexWidget(
        panel.table_view.model().index(0, ACTION_COL))
    assert isinstance(button, QPushButton)
    assert button.isEnabled() is False
    assert button.text() == "确认回写"


def test_stop_draft_cannot_be_confirmed_and_issues_no_write(qapp):
    calls = []
    panel = DispositionPanel(writer=lambda d: calls.append(d) or {"ok": True})
    panel.set_items([STOP_ITEM])
    assert panel.confirm_row(0) is False
    assert panel.confirm_all() == {"attempted": 0, "succeeded": 0, "failed": []}
    assert calls == []
    assert panel.write_request_count() == 0
    assert panel.confirmed_items() == []


def test_draft_body_is_readonly_copyable_and_evidence_on_same_screen(qapp):
    panel = DispositionPanel()
    panel.set_items([STOP_ITEM])
    text = panel.show_draft(0)
    assert text == STOP_ITEM["stop_draft_content"]
    assert panel.draft_edit.toPlainText() == STOP_ITEM["stop_draft_content"]
    assert panel.draft_edit.isReadOnly() is True
    evidence = panel.evidence_edit.toPlainText()
    assert "模型建议由医师复核停嘱" in evidence
    assert "execute_dept_code" in evidence
    assert "长期医嘱执行科室与新病区不一致" in evidence
    assert panel.copy_draft(0) == STOP_ITEM["stop_draft_content"]
    assert panel.last_copied_draft == STOP_ITEM["stop_draft_content"]
    assert panel.copy_draft_button.text() == "复制草稿"


def test_no_stop_category_row_has_red_label_and_no_button(qapp):
    panel = DispositionPanel()
    panel.set_items([NO_STOP_ITEM])
    assert panel.no_stop_rows() == [0]
    assert panel.status_text_for(0) == tokens.NO_STOP_LABEL == "不可自动停嘱·需医师判断"
    label = panel.table_view.indexWidget(
        panel.table_view.model().index(0, STATUS_COL))
    assert isinstance(label, QLabel)
    assert label.text() == "不可自动停嘱·需医师判断"
    assert tokens.COLORS["danger_text"] in label.styleSheet()
    action = panel.table_view.indexWidget(
        panel.table_view.model().index(0, ACTION_COL))
    assert isinstance(action, QLabel)          # 无按钮
    assert not isinstance(action, QPushButton)


def test_no_stop_category_produces_no_draft(qapp):
    panel = DispositionPanel()
    panel.set_items([STOP_ITEM, NO_STOP_ITEM])
    assert panel.draft_text_for(0) == STOP_ITEM["stop_draft_content"]
    assert panel.draft_text_for(1) == ""        # 命中不可停品类不产出草稿
    assert panel.show_draft(1) == ""
    assert panel.draft_edit.toPlainText() == ""
    assert panel.confirm_row(1) is False


def test_no_submit_or_sign_buttons_in_ui(qapp):
    """界面与托盘按钮文案中无提交 / 签名入口。"""
    panel = DispositionPanel()
    panel.set_items([STOP_ITEM, NO_STOP_ITEM])
    buttons = [b.text() for b in panel.findChildren(QPushButton)]
    for text in buttons:
        assert "提交" not in text
        assert "签名" not in text
    window = MainWindow(qapp=qapp)
    for text in [b.text() for b in window.findChildren(QPushButton)]:
        assert "提交" not in text
        assert "签名" not in text
    tray = TrayController()
    assert tuple(tray.menu_items()) == MENU_ITEMS
    for text in tray.menu_items():
        assert "提交" not in text
        assert "签名" not in text


def test_static_scan_finds_no_forbidden_calls_in_ui_sources():
    """S 组 guards 的静态扫描：ui/ 源码中不存在提交 / 签名调用路径。"""
    files = sorted(p for p in SRC_UI.rglob("*.py") if p.is_file())
    assert files, "ui/ 目录应有源码文件"
    for path in files:
        source = path.read_text(encoding="utf-8")
        assert guards.find_forbidden_calls(source) == [], f"{path.name} 检出禁用调用"
    assert guards.assert_no_submit_paths(files) is True
    assert guards.assert_no_submit_paths() is True
