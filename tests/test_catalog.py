"""R 组：处置动作类型目录（任务 46）。

覆盖：内置 4 类与 G1 NODE_TYPES 共用定义（只读、不修改 G1）、信息科 JSON
追加类型后 refresh 可见、未知类型兜底标为待人工处置、未注册类型不整图失败
且其余节点照常执行、损坏 JSON 明确报错。
"""
from __future__ import annotations

import json

import pytest

from itc.catalog.registry import ActionCatalogError, ActionType, ActionTypeRegistry
from itc.graph.registry import NODE_TYPES


def test_builtin_four_types_share_g1_definitions():
    reg = ActionTypeRegistry()
    ids = {t.type_id for t in reg.all_types()}
    assert {"change_site", "specimen_route", "bed_fee_hint", "stop_draft"}.issubset(ids)
    assert len(reg.all_types()) >= 4
    for type_id in ("change_site", "specimen_route", "bed_fee_hint", "stop_draft"):
        at = reg.get(type_id)
        assert at is not None
        assert at.display_name == NODE_TYPES[type_id].display_name
        assert at.writable == NODE_TYPES[type_id].writable
    # 停嘱草稿只到 DRAFT
    assert reg.get("stop_draft").default_params["status"] == "DRAFT"


def test_custom_json_type_visible_after_refresh(tmp_path):
    p = tmp_path / "custom_action_types.json"
    p.write_text(json.dumps({"action_types": []}, ensure_ascii=False), encoding="utf-8")
    reg = ActionTypeRegistry(custom_path=str(p))
    assert reg.get("custom_infusion_room") is None

    p.write_text(json.dumps({
        "action_types": [
            {
                "type_id": "custom_infusion_room",
                "display_name": "输液室配药核对",
                "applies_to": "order",
                "writable": False,
                "default_params": {"need_nurse_check": True},
            },
        ]
    }, ensure_ascii=False), encoding="utf-8")
    reg.refresh()
    at = reg.get("custom_infusion_room")
    assert at is not None
    assert at.display_name == "输液室配药核对"
    assert at.default_params["need_nurse_check"] is True
    assert "custom_infusion_room" in {t.type_id for t in reg.all_types()}
    # 只在本目录追加，不写回 G1 的 NODE_TYPES
    assert "custom_infusion_room" not in NODE_TYPES


def test_register_and_as_dict():
    reg = ActionTypeRegistry()
    at = reg.register(ActionType("x_probe", "探查", "specimen", False, {"k": 1}))
    assert reg.get("x_probe") is at
    d = at.as_dict()
    assert d["type_id"] == "x_probe" and d["default_params"] == {"k": 1}
    reg.refresh()
    assert reg.get("x_probe") is not None  # 显式登记刷新后仍保留


def test_unknown_type_fallback_marks_manual():
    reg = ActionTypeRegistry()
    assert reg.get("ghost_node") is None
    fb = reg.unknown_type_fallback("ghost_node")
    assert isinstance(fb, ActionType)
    assert "待人工处置" in fb.display_name
    assert fb.writable is False
    assert fb.default_params["manual"] is True
    assert "ghost_node" in fb.default_params["reason"]


def test_unknown_type_does_not_fail_whole_graph():
    reg = ActionTypeRegistry()
    # 图中混合已注册节点（取数 / 处置动作）与一个未注册类型
    node_types = ["fetch_orders", "change_site", "ghost_node", "bed_fee_hint"]
    plan = reg.classify_types(node_types)
    assert plan["graph_ok"] is True
    assert "change_site" in plan["executable"]
    assert "bed_fee_hint" in plan["executable"]
    assert "fetch_orders" in plan["executable"]
    assert plan["manual"] == ["ghost_node"]
    assert "ghost_node" in plan["fallbacks"]
    assert plan["fallbacks"]["ghost_node"]["default_params"]["manual"] is True
    # 其余节点照常执行：可执行清单不含未知节点，数量 = 已知节点数
    assert len(plan["executable"]) == len(node_types) - 1
    assert "ghost_node" not in plan["executable"]


def test_broken_custom_json_raises_clear_error(tmp_path):
    p = tmp_path / "custom_action_types.json"
    p.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ActionCatalogError):
        ActionTypeRegistry(custom_path=str(p))
