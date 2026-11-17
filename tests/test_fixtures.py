"""任务 7 · fixtures 边界样例：只留 6 个 edge + 4 个 sample、占位脱敏、6 类边界可断言。

只用 fixtures_dir fixture；不 import H/M/U/S 组模块，不改 pytest.ini / conftest.py。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from itc.domain.models import ExecutingOrder, FieldMissingError

EXPECTED_SAMPLES = {
    "transfer_sample.json",
    "orders_sample.json",
    "specimens_sample.json",
    "charges_sample.json",
}

EXPECTED_EDGES = {
    "edge_missing_execute_dept.json",
    "edge_specimen_ambiguous.json",
    "edge_no_executing_orders.json",
    "edge_duplicate_transfer_no.json",
    "edge_charge_dept_mismatch.json",
    "edge_no_stop_hit.json",
}

# 拟真住院号 / 身份证 / 联系方式是纯长数字串；占位标识与带分隔符的样例都不匹配
_REALISTIC_ID = re.compile(r"^\d{8,18}$")
_PHI_KEYS = {
    "name", "patient_name", "real_name", "full_name",
    "id_no", "id_card", "idcard", "id_number", "identity_no", "identity",
    "ssn", "social_id", "phone", "phone_no", "mobile", "tel", "telephone",
    "contact", "contact_no", "address", "home_address",
}


def _load(fixtures_dir: Path, name: str) -> dict:
    return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))


def _assert_no_phi(obj) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert str(key).strip().lower() not in _PHI_KEYS, f"出现 PHI 字段：{key}"
            _assert_no_phi(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_phi(item)
    elif isinstance(obj, str):
        assert not _REALISTIC_ID.match(obj.strip()), f"出现拟真标识：{obj}"


class TestFixturesInventory:
    def test_only_boundary_samples_remain(self, fixtures_dir):
        json_files = {p.name for p in fixtures_dir.glob("*.json")}
        edges = {p.name for p in fixtures_dir.glob("edge_*.json")}
        samples = {p.name for p in fixtures_dir.glob("*_sample.json")}
        assert len(edges) == 6
        assert samples == EXPECTED_SAMPLES
        assert edges | samples == json_files  # 无其它冗余 JSON

    def test_all_fixtures_parse_and_are_redacted(self, fixtures_dir):
        for path in sorted(fixtures_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            _assert_no_phi(data)

    def test_original_samples_keep_expected_keys(self, fixtures_dir):
        transfer = _load(fixtures_dir, "transfer_sample.json")
        for key in ("transfer_no", "inpatient_no", "from_dept_code", "to_dept_code",
                    "ward_code", "transfer_at"):
            assert key in transfer
        for name in ("orders_sample.json", "specimens_sample.json", "charges_sample.json"):
            assert "items" in _load(fixtures_dir, name)


class TestEdgeCases:
    def test_missing_execute_dept(self, fixtures_dir):
        d = _load(fixtures_dir, "edge_missing_execute_dept.json")
        assert d["case"] == "missing_execute_dept"
        order = d["orders"][0]
        assert "execute_dept_code" not in order  # 边界：字段缺失
        assert d["expected"]["auto_disposition"] == 0
        assert d["expected"]["write_requests"] == 0
        # 用底座严格解析，缺字段必须抛 FieldMissingError（不以默认值兜底）
        with pytest.raises(FieldMissingError):
            ExecutingOrder.from_dict(order)

    def test_specimen_ambiguous(self, fixtures_dir):
        d = _load(fixtures_dir, "edge_specimen_ambiguous.json")
        assert d["case"] == "specimen_ambiguous"
        candidates = d["specimens"][0]["candidate_dept_codes"]
        assert len(candidates) > 1  # 候选 >1 -> 拒绝自动处置
        assert d["expected"]["manual"] is True
        assert d["expected"]["write_requests"] == 0

    def test_no_executing_orders(self, fixtures_dir):
        d = _load(fixtures_dir, "edge_no_executing_orders.json")
        assert d["case"] == "no_executing_orders"
        assert d["orders"] == []  # 零在执行长嘱 -> 不生成 change_site
        assert d["expected"]["change_site_nodes"] == 0
        assert d["expected"]["has_specimen_route"] is True

    def test_duplicate_transfer_no(self, fixtures_dir):
        d = _load(fixtures_dir, "edge_duplicate_transfer_no.json")
        assert d["case"] == "duplicate_transfer_no"
        nos = [t["transfer_no"] for t in d["transfers"]]
        assert len(nos) == 2
        assert len(set(nos)) == 1  # 同批次重复单号
        assert d["expected"]["kept"] == 1
        assert d["expected"]["dropped"] == 1
        assert d["expected"]["drop_reason"] == "duplicate_transfer_no"

    def test_charge_dept_mismatch(self, fixtures_dir):
        d = _load(fixtures_dir, "edge_charge_dept_mismatch.json")
        assert d["case"] == "charge_dept_mismatch"
        to_dept = d["transfer"]["to_dept_code"]
        mismatched = [c for c in d["charges"] if c["dept_code"] != to_dept]
        assert mismatched  # 归属与科别不一致
        assert d["expected"]["not_a_violation"] is True  # 只提示不判违规
        assert "换算" in d["expected"]["disclaimer"]

    def test_no_stop_hit(self, fixtures_dir):
        d = _load(fixtures_dir, "edge_no_stop_hit.json")
        assert d["case"] == "no_stop_hit"
        assert d["orders"][0]["no_stop_category"] == "输血"
        assert d["expected"]["no_stop_hit"] is True
        assert d["expected"]["stop_draft_count"] == 0  # 不生成停嘱草稿
        assert d["expected"]["in_writable_list"] is False
