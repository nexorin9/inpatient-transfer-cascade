"""任务 44（托盘部分）：三态、数量提示、菜单、点击恢复窗口。"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QSystemTrayIcon

from itc.ui.tray import MENU_ITEMS, TRAY_STATES, TrayController


def test_tray_starts_idle(qapp):
    tray = TrayController()
    assert tray.state() == "idle"
    assert tray.counts() == {"awaiting": 0, "retry": 0}
    assert "空闲" in tray.tooltip_text()
    assert TRAY_STATES == ("idle", "awaiting", "retry")


def test_three_states_driven_by_counts(qapp):
    tray = TrayController()
    assert tray.set_counts(awaiting=3, retry=0) == "awaiting"
    assert tray.state() == "awaiting"
    assert "待确认" in tray.tooltip_text()
    assert tray.set_counts(awaiting=0, retry=2) == "retry"
    assert tray.state() == "retry"
    assert "待重试" in tray.tooltip_text()
    assert tray.set_counts(0, 0) == "idle"
    assert tray.state() == "idle"
    # 待重试优先级高于待确认
    assert tray.set_counts(awaiting=5, retry=1) == "retry"


def test_counts_and_badge_text(qapp):
    tray = TrayController()
    changed = []
    tray.countsChanged.connect(lambda a, r: changed.append((a, r)))
    tray.set_counts(awaiting=4, retry=1)
    assert tray.counts() == {"awaiting": 4, "retry": 1}
    assert tray.badge_text() == "待确认 4 / 待重试 1"
    assert "待确认 4 / 待重试 1" in tray.tooltip_text()
    assert changed[-1] == (4, 1)
    assert tray.set_counts(awaiting=-3, retry=-1) == "idle"
    assert tray.counts() == {"awaiting": 0, "retry": 0}


def test_menu_contains_exactly_four_items(qapp):
    tray = TrayController()
    assert tray.menu_items() == list(MENU_ITEMS)
    assert len(tray.menu_items()) == 4
    for text in tray.menu_items():
        assert "提交" not in text and "签名" not in text


def test_action_handlers_and_quit_signal(qapp):
    tray = TrayController()
    seen = []
    quits = []
    tray.set_action_handler("拉取今日转科", lambda: seen.append("pull"))
    tray.set_action_handler("待确认清单", lambda: seen.append("awaiting"))
    tray.quitRequested.connect(lambda: quits.append(True))
    for text in MENU_ITEMS:
        tray.trigger_menu(text)
    assert seen == ["pull", "awaiting"]
    assert quits == [True]
    tray.set_action_handler("拉取今日转科", None)
    tray.trigger_menu("拉取今日转科")
    assert seen == ["pull", "awaiting"]


def test_click_restores_window(qapp):
    tray = TrayController()
    restored = []
    signals = []
    tray.on_restore = lambda: restored.append(True)
    tray.restoreRequested.connect(lambda: signals.append(True))
    tray._on_activated(QSystemTrayIcon.Trigger)
    assert restored == [True] and signals == [True]
    tray.request_restore()
    assert len(restored) == 2
    # Context（右键菜单）不误触恢复
    tray._on_activated(QSystemTrayIcon.Context)
    assert len(restored) == 2
