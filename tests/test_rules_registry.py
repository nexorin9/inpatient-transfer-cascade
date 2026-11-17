"""R 组：规则目录加载与版本（任务 10）。

覆盖：每条规则声明 id/version/enabled/hit_count、版本与开关、命中计数、
单文件损坏保留其余并报告 failed、判定 rule_id 与目录 id 对应。
"""
from __future__ import annotations

import json

from itc.rules.registry import RuleRegistry


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_default_catalog_declares_id_version_enabled_hit_count():
    reg = RuleRegistry()
    rules = reg.load()
    assert isinstance(rules, list) and rules
    for rule in rules:
        assert rule.get("id")
        assert rule.get("version")
        assert "enabled" in rule
        assert "hit_count" in rule
    assert reg.failed == []


def test_version_enabled_and_hit_counts(tmp_path):
    _write(tmp_path, "rule_a.json", {"id": "R_A", "version": "2.1", "enabled": True})
    _write(tmp_path, "rule_off.json", {"id": "R_OFF", "version": "2.1", "enabled": False})
    reg = RuleRegistry(rules_dir=tmp_path)
    rules = reg.load()
    assert len(rules) == 2
    assert reg.version == "1.0"  # 无 rules.json 时用默认目录版本
    assert reg.version() == "1.0"  # 兼容方法式调用
    assert reg.version_of("R_A") == "2.1"
    assert reg.enabled("R_A") is True
    assert reg.enabled("R_OFF") is False
    assert reg.enabled("NOT_DECLARED") is False

    assert reg.hit("R_A") == 1
    assert reg.hit("R_A") == 2
    assert reg.hit("R_A") == 3
    assert reg.hit_counts()["R_A"] == 3
    assert reg.hit_counts()["R_OFF"] == 0
    assert any(r["hit_count"] == 3 for r in reg.load())  # 重新加载不清零命中计数


def test_single_corrupted_file_keeps_others_and_reports_failed(tmp_path):
    _write(tmp_path, "rule_a.json", {"id": "R_A", "version": "1", "enabled": True})
    _write(tmp_path, "rule_b.json", {"id": "R_B", "version": "1", "enabled": True})
    (tmp_path / "rule_bad.json").write_text("{ broken", encoding="utf-8")
    reg = RuleRegistry(rules_dir=tmp_path)
    rules = reg.load()
    assert {r["id"] for r in rules} == {"R_A", "R_B"}
    assert len(reg.failed) == 1
    assert reg.failed[0]["file"] == "rule_bad.json"
    assert reg.failed[0]["error"]


def test_rules_json_catalog_version_and_ids_cover_all_judgements():
    reg = RuleRegistry()
    ids = {r["id"] for r in reg.load()}
    expected = {
        "EXEC_SITE_CONSISTENT", "EXEC_SITE_MAP_EXPLAINED", "EXEC_SITE_HARD_CONFLICT",
        "SPECIMEN_ROUTE_UNIQUE", "SPECIMEN_ROUTE_AMBIGUOUS",
        "NO_STOP_CATALOG_HIT", "NO_STOP_CATALOG_ERROR",
        "BED_FEE_CONSISTENT", "BED_FEE_OWNERSHIP_DOUBT", "BED_FEE_TRANSFER_DAY",
    }
    assert expected.issubset(ids)
    assert reg.version == "1.0"
    assert all(reg.enabled(x) for x in expected)


def test_judgement_rule_ids_all_declared_in_catalog():
    from itc.rules.bed_fee import bed_fee_hints
    from itc.rules.conflicts import judge_execution_site
    from itc.rules.dept_map import load_dept_map
    from itc.rules.no_stop import is_no_stop
    from itc.rules.specimen_route import judge_specimen_route

    ids = {r["id"] for r in RuleRegistry().load()}
    order = {
        "order_no": "<O1>", "drug_name": "输血用红细胞悬液", "content": "<内容>",
        "route": "静滴", "frequency": "qd", "execute_dept_code": "<DEPT_CODE_FROM>",
        "start_at": "2026-01-01T00:00:00+08:00",
    }
    transfer = {
        "transfer_no": "<T1>", "inpatient_no": "<INPATIENT_NO>",
        "from_dept_code": "<DEPT_CODE_FROM>", "to_dept_code": "<DEPT_CODE_TO>",
        "from_dept_name": "<原科室名>", "to_dept_name": "<目标科室名>",
        "ward_code": "<WARD_CODE>", "transfer_at": "2026-01-01T09:00:00+08:00",
    }
    specimen = {
        "specimen_no": "<S1>", "test_name": "凝血四项",
        "collect_dept_code": "<DEPT_CODE_FROM>", "status": "pending",
    }
    charge = {
        "category": "bed", "fee_date": "2025-12-31", "amount": "45.00",
        "dept_code": "<DEPT_CODE_FROM>",
    }
    produced = {
        judge_execution_site(order, transfer, load_dept_map()).rule_id,
        judge_specimen_route(specimen, transfer, candidates=["检验科"]).rule_id,
        judge_specimen_route(specimen, transfer, candidates=["A", "B"]).rule_id,
        is_no_stop(order).rule_id,
        bed_fee_hints([charge], transfer)[0].rule_id,
    }
    assert produced.issubset(ids)
