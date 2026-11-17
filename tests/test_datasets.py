"""任务 7 · 数据集：固定种子确定性、规模、脱敏、溯源、--check 校验。

只用 project_root fixture；不 import H/M/U/S 组模块，不改 pytest.ini / conftest.py。
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

# 只扫描字符串值：拟真住院号 / 身份证 / 电话是纯长数字串；日历日期单独在数据集层断言
_REALISTIC_ID = re.compile(r"^\d{8,18}$")
_CALENDAR_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PHI_KEYS = {
    "name", "patient_name", "real_name", "full_name",
    "id_no", "id_card", "idcard", "id_number", "identity_no", "identity",
    "ssn", "social_id", "phone", "phone_no", "mobile", "tel", "telephone",
    "contact", "contact_no", "address", "home_address",
}


def _load_tool(project_root: Path, mod_name: str, rel: str):
    path = project_root / rel
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fetch(project_root):
    return _load_tool(project_root, "_d_fetch_datasets", "tools/fetch_datasets.py")


def _scan(obj, *, forbid_calendar_date=False):
    """递归扫描：无 PHI 字段、无拟真长数字标识、按要求无日历日期。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert str(key).strip().lower() not in _PHI_KEYS, f"出现 PHI 字段：{key}"
            _scan(value, forbid_calendar_date=forbid_calendar_date)
    elif isinstance(obj, list):
        for item in obj:
            _scan(item, forbid_calendar_date=forbid_calendar_date)
    elif isinstance(obj, str):
        assert not _REALISTIC_ID.match(obj.strip()), f"出现拟真标识：{obj}"
        if forbid_calendar_date:
            assert not _CALENDAR_DATE.search(obj), f"出现日历日期：{obj}"


class TestGenerateDeterminism:
    def test_same_seed_identical_scale_and_fields(self, fetch):
        d1 = fetch.generate_dataset(20261108)
        d2 = fetch.generate_dataset(20261108)
        assert d1 == d2  # 规模与字段逐项一致
        assert d1 is not d2  # 确为两次独立生成，非同一对象恒真

    def test_different_seed_produces_different_data(self, fetch):
        assert fetch.generate_dataset(1) != fetch.generate_dataset(2)

    def test_scale_thresholds(self, fetch):
        data = fetch.generate_dataset(fetch.DEFAULT_SEED)
        for category, minimum in fetch.MIN_SCALE.items():
            assert len(data[category]) >= minimum, f"{category} 未达标"


class TestNoPhi:
    def test_generated_dataset_has_no_phi_or_realistic_ids(self, fetch):
        data = fetch.generate_dataset(fetch.DEFAULT_SEED)
        # 数据集层连日历日期都不出现（时间一律 <TS:...> 占位）
        for category, items in data.items():
            _scan(items, forbid_calendar_date=True)

    def test_meta_provenance_is_honest(self, fetch):
        meta = fetch._meta("transfers", 240, seed=1, degraded=True)
        assert meta["synthea_export"] is False
        assert meta["mimic_downloaded"] is False
        assert meta["offline_degraded"] is True
        assert meta["source"] == fetch.SOURCE_LABEL
        assert "非 Synthea 导出" in meta["source"]


class TestWriteAndReadme:
    def test_write_dataset_files_and_determinism(self, fetch, tmp_path):
        data = fetch.generate_dataset(7)
        written = fetch.write_dataset(data, tmp_path, seed=7, degraded=True)
        assert set(written) == set(fetch.CATEGORY_FILE)
        for path in written.values():
            assert path.is_file()
        report = fetch.check(tmp_path)
        assert report["files_complete"] and report["all_pass"]
        before = (tmp_path / "orders.json").read_bytes()
        fetch.write_dataset(fetch.generate_dataset(7), tmp_path, seed=7, degraded=True)
        assert (tmp_path / "orders.json").read_bytes() == before

    def test_check_reports_scale_and_completeness(self, fetch, project_root):
        fetch.run(project_root / "data", offline=True, seed=fetch.DEFAULT_SEED)
        report = fetch.check(project_root / "data")
        assert report["files_complete"] is True
        assert report["all_pass"] is True
        for category, minimum in fetch.MIN_SCALE.items():
            assert report["categories"][category]["count"] >= minimum
        prov = report["provenance"]
        assert prov["source"] == fetch.SOURCE_LABEL
        assert prov["synthea_export"] is False

    def test_readme_covers_required_topics(self, fetch, project_root):
        fetch.run(project_root / "data", offline=True, seed=fetch.DEFAULT_SEED)
        text = (project_root / "data" / "README.md").read_text(encoding="utf-8")
        for keyword in ("来源", "许可证", "规模", "字段贴合度", "离线降级",
                        "本地合成（非 Synthea 导出）", "断网"):
            assert keyword in text, f"README 缺 {keyword}"


class TestOfflineDegradation:
    def test_try_public_sample_without_url_degrades(self, fetch, monkeypatch):
        monkeypatch.delenv("ITC_SAMPLE_URL", raising=False)
        res = fetch.try_public_sample(timeout=0.1)
        assert res["ok"] is False
        assert "离线降级" in res["reason"] or "ITC_SAMPLE_URL" in res["reason"]

    def test_offline_run_marks_degradation_and_reuses(self, fetch, tmp_path):
        # 首次离线生成
        r1 = fetch.run(tmp_path, offline=True, seed=5)
        assert r1["degraded"] is True
        assert r1["source"] == fetch.SOURCE_LABEL
        assert r1["check"]["all_pass"] is True
        # 二次离线应复用已有数据，不重写
        r2 = fetch.run(tmp_path, offline=True, seed=5)
        assert r2["reused"] is True
        assert r2["degraded"] is True


class TestCheckCommand:
    def test_check_subprocess(self, project_root):
        proc = subprocess.run(
            [sys.executable, "tools/fetch_datasets.py", "--check"],
            cwd=str(project_root), capture_output=True, text=True, timeout=60,
        )
        out = proc.stdout
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "CHECK PASS" in out
        assert "四类文件齐全：是" in out
        assert "四类规模全部达标：是" in out
        assert out.count("达标") >= 4
        assert "离线降级" in out
        assert "本地合成（非 Synthea 导出）" in out
        assert "Synthea 导出：False" in out
