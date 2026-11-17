"""图模板：版本迁移幂等、未知版本拒收并给可读原因、来源记录、入图记录模板 id/版本。"""
from __future__ import annotations

import json

import pytest

from itc.graph import template
from itc.graph.engine import Engine


def test_default_template_has_four_node_categories():
    nodes = template.DEFAULT_TEMPLATE["nodes"]
    sources = {n.get("source") for n in nodes.values()}
    # 取数 / 判定 / 写回 / 阻断 四类齐全
    assert {"fetch", "judge", "writeback", "block"} <= sources
    assert "external_block" in nodes


def test_load_template_from_dict_and_file(tmp_path):
    raw = {"template_id": "custom/a", "template_version": "1", "nodes": {}}
    assert template.load_template(raw)["template_id"] == "custom/a"
    p = tmp_path / "tpl.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert template.load_template(str(p))["template_version"] == "1"


def test_migrate_template_is_idempotent():
    legacy = {
        "template_id": "legacy/a",
        "version": "1",
        "steps": [
            {"id": "f", "type": "FetchOrders"},
            {"id": "j", "type": "RuleJudge"},
        ],
    }
    once = template.migrate_template(legacy)
    twice = template.migrate_template(once)
    assert once == twice                      # 连跑两次结果一致
    assert once["template_version"] == template.TEMPLATE_VERSION
    assert once["nodes"]["f"]["class_type"] == "fetch_orders"  # 旧类名已迁移
    assert once["meta"]["migrated_from"] == "1"


def test_current_version_migrate_is_noop_equivalent():
    cur = template.migrate_template(template.DEFAULT_TEMPLATE)
    assert cur["template_version"] == template.TEMPLATE_VERSION
    assert template.migrate_template(cur) == cur


def test_unknown_version_rejected_with_readable_reason():
    with pytest.raises(template.TemplateVersionError) as exc:
        template.migrate_template({"template_id": "x", "template_version": "999"})
    msg = str(exc.value)
    assert "999" in msg
    assert template.TEMPLATE_VERSION in msg   # 可读原因含当前支持版本


def test_template_source_returns_id_and_version():
    tid, ver = template.template_source(
        {"template_id": "custom/a", "template_version": "1"}
    )
    assert tid == "custom/a" and ver == "1"
    tid2, ver2 = template.template_source(template.DEFAULT_TEMPLATE)
    assert tid2 == template.DEFAULT_TEMPLATE_ID
    assert ver2 == template.TEMPLATE_VERSION


def test_every_submit_records_template_id_and_version(db_conn):
    e = Engine(db_conn, template_version=template.TEMPLATE_VERSION)
    g = {"A": {"class_type": "fetch", "inputs": {}}}
    assert e.submit("tpl-run", g, transfer_no="T-1") is True
    row = db_conn.execute(
        "SELECT template_id,template_version FROM graph_runs WHERE run_id='tpl-run'"
    ).fetchone()
    assert row["template_id"] == template.DEFAULT_TEMPLATE_ID
    assert row["template_version"] == template.TEMPLATE_VERSION
