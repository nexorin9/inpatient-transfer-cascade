"""任务 45：加载 / 空 / 错误三态、toast、二次确认、两档宽度，以及发布清理断言。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QLabel, QTableView, QWidget

from itc.store import guards
from itc.ui import states

SRC_UI = Path(__file__).resolve().parents[1] / "src" / "itc" / "ui"

# 占位文案分片拼接，避免测试源码自身被全局 grep 命中
PLACEHOLDER_TEXT = "（Phase 4" + " 后续 task 补齐）"
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]")


def _ui_sources() -> list[Path]:
    return sorted(p for p in SRC_UI.rglob("*.py") if p.is_file())


def test_loading_state_renders_and_blocks_repeat_clicks(qapp):
    placeholder = states.StatePlaceholder()
    for phase, copy in states.LOADING_COPY.items():
        placeholder.show_loading(phase)
        assert placeholder.state() == "loading"
        assert placeholder.title_label.text() == copy
        assert placeholder.is_busy() is True
        assert placeholder.action_button.isEnabled() is False
        assert placeholder.property("loading") == "true"
    placeholder.set_busy(False)
    assert placeholder.is_busy() is False
    assert placeholder.action_button.isEnabled() is True


def test_three_empty_states_have_distinct_copy(qapp):
    placeholder = states.StatePlaceholder()
    texts = []
    for key, (title, hint) in states.EMPTY_COPY.items():
        placeholder.show_empty(key)
        assert placeholder.state() == "empty"
        assert placeholder.title_label.text() == title
        assert placeholder.detail_label.text() == hint
        assert placeholder.action_button.isVisible() is False
        texts.append(title)
    assert set(texts) == {"今日暂无转科单", "本次转科无牵动项", "处置清单为空"}
    placeholder.show_empty("custom 文案")
    assert placeholder.title_label.text() == "custom 文案"


def test_error_state_distinguishes_retryable(qapp):
    placeholder = states.StatePlaceholder()
    placeholder.show_error("his")
    assert placeholder.state() == "error"
    assert placeholder.title_label.text() == "HIS 不可达"
    assert "可重试" in placeholder.detail_label.text()
    assert placeholder.action_button.isEnabled() is True
    placeholder.show_error("model")
    assert placeholder.title_label.text() == "模型服务不可达"
    assert "不可自动重试" in placeholder.detail_label.text()
    assert placeholder.action_button.isEnabled() is False
    placeholder.show_error("writeback", retryable=True, suggestion="改用待重写队列")
    assert "改用待重写队列" in placeholder.detail_label.text()


def test_error_retry_callback(qapp):
    placeholder = states.StatePlaceholder()
    placeholder.show_error("his")
    seen = []
    placeholder.retry_callback = lambda: seen.append(True)
    placeholder.action_button.click()
    assert seen == [True]


def test_three_states_are_declared():
    assert states.THREE_STATES == ("loading", "empty", "error")
    assert set(states.LOADING_COPY) == {"fetch", "judge", "writeback"}
    assert set(states.ERROR_COPY) == {"his", "model", "writeback", "import"}


def test_toast_feedback(qapp):
    states.clear_toast_history()
    parent = QWidget()
    toast = states.show_toast(parent, "已写回 HIS 7 条，导出交接核对单交给病区")
    assert isinstance(toast, QWidget)
    label = toast.findChild(QLabel, "toastText")
    assert label is not None
    assert label.text() == "已写回 HIS 7 条，导出交接核对单交给病区"
    assert states.toast_history() == ("已写回 HIS 7 条，导出交接核对单交给病区",)
    states.show_toast(parent, "第二条反馈")
    assert len(states.toast_history()) == 2


def test_confirm_destructive_with_injected_answer(qapp):
    with states.auto_answer(True):
        assert states.confirm_destructive(None, "清空清单？") is True
    with states.auto_answer(False):
        assert states.confirm_destructive(None, "批量重跑？") is False
    # 无头环境无注入时按安全默认拒绝，不阻塞
    states.set_auto_answer(None)
    assert states.confirm_destructive(None, "退出未确认项？") is False
    states.set_auto_answer(None)


def test_apply_width_two_tiers_fold_columns(qapp):
    table = QTableView()
    table.setModel(QStandardItemModel(3, 8))
    host = QWidget()
    host.table_view = table
    assert states.apply_width(host, 768) == "narrow"
    assert [table.isColumnHidden(c) for c in range(8)] == [
        False, False, False, False, True, True, True, True]
    assert table.horizontalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    assert states.apply_width(host, 1280) == "wide"
    assert all(not table.isColumnHidden(c) for c in range(8))
    assert host.property("width_tier") == "wide"
    assert states.WIDTH_CHOICES == (1280, 768)
    assert states.width_tier(768) == "narrow" and states.width_tier(1280) == "wide"


def test_no_placeholder_copyleft_in_ui_sources():
    for path in _ui_sources():
        source = path.read_text(encoding="utf-8")
        assert PLACEHOLDER_TEXT not in source, f"{path.name} 残留占位文案"


def test_no_emoji_in_ui_sources():
    for path in _ui_sources():
        source = path.read_text(encoding="utf-8")
        assert EMOJI_RE.search(source) is None, f"{path.name} 含 emoji 图标"


def test_no_forbidden_submit_paths_in_ui_sources():
    files = _ui_sources()
    assert files
    for path in files:
        assert guards.find_forbidden_calls(path.read_text(encoding="utf-8")) == []
    assert guards.assert_no_submit_paths(files) is True


def test_ui_sources_do_not_import_h_or_m_or_web_frameworks():
    for path in _ui_sources():
        source = path.read_text(encoding="utf-8")
        for forbidden in ("from itc.his", "import itc.his",
                          "from itc.model_gateway", "import itc.model_gateway",
                          "react", "vue.js", "tailwind", "cdn"):
            assert forbidden not in source, f"{path.name} 含越界依赖 {forbidden}"
