"""任务 13：设计 token 视觉地基的真实断言。"""
from __future__ import annotations

import re

import pytest
from PySide6.QtWidgets import QWidget

from itc.domain.models import STATES
from itc.ui import tokens as T

REQUIRED_COLORS = {
    "primary": "#1B6AC9",
    "primary_hover": "#15579F",
    "primary_pressed": "#124A87",
    "success": "#16A34A",
    "warning": "#D97706",
    "danger": "#DC2626",
    "info": "#2563EB",
    "neutral_title": "#0F172A",
    "neutral_body": "#334155",
    "neutral_helper": "#64748B",
    "neutral_disabled": "#94A3B8",
    "neutral_border": "#E2E8F0",
    "neutral_header": "#F1F5F9",
    "neutral_zebra": "#F8FAFC",
    "page_bg": "#F1F5F9",
    "card_bg": "#FFFFFF",
    "success_bg": "#DCFCE7",
    "success_text": "#166534",
    "warning_bg": "#FEF3C7",
    "warning_text": "#92400E",
    "danger_bg": "#FEE2E2",
    "danger_text": "#991B1B",
    "info_bg": "#DBEAFE",
    "info_text": "#1E40AF",
}

_HEX_RE = re.compile(r"^#[0-9A-F]{6}$")


def test_tokens_importable_and_keys_complete():
    assert isinstance(T.TOKENS, dict)
    for key in ("colors", "spacing", "fonts", "status_colors", "state_labels",
                "button_qss", "input_qss", "table_qss", "full_qss"):
        assert key in T.TOKENS
    # 扁平别名与嵌套色板等价
    assert T.TOKENS["primary"] == T.TOKENS["colors"]["primary"]


def test_required_color_values_from_visual_card():
    for key, value in REQUIRED_COLORS.items():
        assert T.COLORS[key] == value, f"{key} 应取 {value}"
        assert T.TOKENS[key] == value
    assert T.PRIMARY == "#1B6AC9"
    assert T.PRIMARY_HOVER == "#15579F"
    assert T.PRIMARY_PRESSED == "#124A87"
    assert T.SUCCESS == "#16A34A"
    assert T.WARNING == "#D97706"
    assert T.DANGER == "#DC2626"
    assert T.INFO == "#2563EB"
    assert T.NEUTRAL_TITLE == "#0F172A"


def test_all_colors_fall_in_allowed_set():
    assert set(T.COLORS.values()) == set(T.ALLOWED_COLORS)
    for value in T.COLORS.values():
        assert _HEX_RE.match(value), f"非法色值 {value}"
        assert value in T.ALLOWED_COLORS
    assert set(REQUIRED_COLORS.values()) <= set(T.ALLOWED_COLORS)


def test_spacing_grid_only_allowed_values():
    assert T.SPACING == (4, 8, 12, 16, 24, 32)
    assert T.ALLOWED_SPACING == (4, 8, 12, 16, 24, 32)
    assert all(v in T.SPACING for v in T.ALLOWED_SPACING)
    # 所有间距角色取值都必须落在 8px 栅格集合内
    assert set(T.SPACING_ROLES.values()) <= set(T.SPACING)
    assert T.SPACING_ROLES["page_padding"] == 24
    assert T.SPACING_ROLES["card_padding"] == 16
    assert T.SPACING_ROLES["cell_vertical"] == 8
    assert T.SPACING_ROLES["cell_horizontal"] == 12
    assert T.RADII == {"card": 6, "toast": 8, "modal": 12}
    assert T.LAYOUT["nav_width"] == 200


def test_four_level_font_hierarchy():
    assert T.FONT_LEVELS == ("page_title", "section_title", "body", "helper")
    assert set(T.FONTS) == set(T.FONT_LEVELS)
    assert [T.FONTS[k]["size"] for k in T.FONT_LEVELS] == [20, 16, 14, 12]
    assert [T.FONTS[k]["weight"] for k in T.FONT_LEVELS] == [600, 600, 400, 400]
    assert "PingFang SC" in T.FONT_FAMILY and "sans-serif" in T.FONT_FAMILY
    assert "Courier New" in T.MONO_FAMILY and "monospace" in T.MONO_FAMILY


def test_five_status_colors_and_labels():
    assert T.STATUS_KEYS == ("awaiting", "written", "manual", "blocked", "failed")
    assert T.STATUS_LABELS == {
        "awaiting": "待确认", "written": "已回写", "manual": "待人工判定",
        "blocked": "阻断", "failed": "失败",
    }
    for key in T.STATUS_KEYS:
        spec = T.STATUS_COLORS[key]
        assert spec["label"] == T.STATUS_LABELS[key]
        for field in ("base", "bg", "text", "dot"):
            assert spec[field] in T.ALLOWED_COLORS, f"{key}.{field} 越出色板"
    # 五类状态色互不相同（阻断为深灰，与红/琥珀/蓝/绿区分）
    assert len({T.STATUS_COLORS[k]["dot"] for k in T.STATUS_KEYS}) == 5


def test_state_labels_cover_all_disposition_states():
    assert set(T.STATE_LABELS) == set(STATES)
    assert set(T.STATE_STATUS) == set(STATES)
    assert all(v in T.STATUS_KEYS for v in T.STATE_STATUS.values())
    covered = set()
    for states in T.STATUS_STATES.values():
        covered.update(states)
    assert covered == set(STATES)
    assert T.status_label("awaiting_confirm") == "待确认"
    assert T.status_label("written_back") == "已回写"
    assert T.status_label("failed") == "失败"


def test_manual_status_overlay():
    assert T.resolve_status("awaiting_confirm", {"degraded": True}) == "manual"
    assert T.resolve_status("awaiting_confirm", {"manual_reason": "歧义"}) == "manual"
    assert T.resolve_status("awaiting_confirm", {"judgement": "UNDETERMINED"}) == "manual"
    assert T.status_label("awaiting_confirm", {"degraded": True}) == "待人工判定"
    assert T.resolve_status("written_back", {"degraded": True}) == "written"


def test_node_status_colors_five_visual_classes():
    keys = ("pending", "running", "success", "manual", "failed")
    colors = [T.NODE_STATUS_COLORS[k] for k in keys]
    assert len(set(colors)) == 5
    assert colors[0] == T.COLORS["neutral_disabled"]
    assert colors[1] == T.COLORS["primary"]
    assert colors[2] == T.COLORS["success"]
    assert colors[3] == T.COLORS["warning"]
    assert colors[4] == T.COLORS["danger"]
    assert T.node_status_color("blocked") == T.DANGER
    assert T.node_status_color("不存在的状态") == T.COLORS["neutral_disabled"]


def test_button_input_table_have_all_four_states():
    for name, qss in T.COMPONENT_QSS.items():
        for state in T.COMPONENT_STATES:
            assert state in qss, f"{name} 缺少 {state} 态"
        assert T.COLORS["primary"] in qss
    # loading 用动态属性表达，四态在三类组件里齐全
    assert '[loading="true"]' in T.BUTTON_QSS
    assert '[loading="true"]' in T.INPUT_QSS
    assert '[loading="true"]' in T.TABLE_QSS


def test_status_label_qss_and_draft_qss(qapp):
    for key in T.STATUS_KEYS:
        qss = T.status_qss(key)
        assert "QLabel" in qss
        assert T.STATUS_COLORS[key]["bg"] in qss
        assert T.STATUS_COLORS[key]["text"] in qss
    assert T.COLORS["neutral_header"] in T.DRAFT_LABEL_QSS
    assert T.COLORS["danger_text"] in T.NO_STOP_LABEL_QSS
    assert T.STOP_DRAFT_LABEL == "草稿·需医师复核"
    assert T.NO_STOP_LABEL == "不可自动停嘱·需医师判断"


def test_apply_theme(qapp):
    widget = QWidget()
    qss = T.apply_theme(widget)
    assert isinstance(qss, str) and qss
    assert widget.styleSheet() == qss
    assert "#1B6AC9" in qss and "#15579F" in qss and "#124A87" in qss
