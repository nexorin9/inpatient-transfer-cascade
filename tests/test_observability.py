"""任务 49 · 可观测性：错误码、结构化日志、脱敏断言、批次摘要落盘。

用 project_root / db_conn fixture；不 import H/M/U/S 组模块，不改 pytest.ini / conftest.py。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from itc import observability as obs


REQUIRED_CODES = (
    "FETCH_FAILED",        # 取数失败
    "FIELD_MISSING",       # 字段缺失
    "ENTITY_AMBIGUOUS",    # 实体歧义
    "MODEL_UNREACHABLE",   # 模型不可达
    "WRITEBACK_FAILED",    # 写回失败
    "IDEMPOTENCY_HIT",     # 幂等命中
)


class TestErrorCodes:
    def test_all_six_error_codes_present(self):
        for code in REQUIRED_CODES:
            assert code in obs.ERROR_CODES
        assert len(obs.ERROR_CODES) >= 6
        assert all(isinstance(v, str) and v for v in obs.ERROR_CODES.values())


class TestStructuredLog:
    def test_fields_and_inpatient_digest_only(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ITC_LOG_DIR", str(tmp_path))
        raw_inpatient = "INP-RAW-0000123456"
        line = obs.structured_log({
            "run_id": "R-1",
            "node_id": "fetch_orders:1",
            "node_type": "fetch_orders",
            "status": "ok",
            "duration_ms": 12.5,
            "error_code": "",
            "inpatient_no": raw_inpatient,
        })
        d = json.loads(line)
        for key in ("ts", "run_id", "node_id", "node_type", "status",
                    "duration_ms", "error_code", "inpatient_digest"):
            assert key in d
        assert d["node_id"] == "fetch_orders:1"
        assert d["node_type"] == "fetch_orders"
        assert d["duration_ms"] == 12.5
        assert "inpatient_no" not in d  # 住院号只留摘要
        assert d["inpatient_digest"] == obs.digest_inpatient_no(raw_inpatient)
        assert len(d["inpatient_digest"]) == 12
        assert obs.assert_log_redacted(line) is True

    def test_phi_fields_are_dropped_from_line(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ITC_LOG_DIR", str(tmp_path))
        line = obs.structured_log({
            "node_id": "n", "node_type": "rule_judge", "status": "failed",
            "error_code": "FETCH_FAILED", "name": "<占位姓名>",
            "phone": "<占位联系方式>",
        })
        d = json.loads(line)
        assert "name" not in d and "phone" not in d  # PHI 字段不落日志
        assert obs.assert_log_redacted(line) is True


class TestRedactionAssertion:
    def test_rejects_phi_and_raw_inpatient(self):
        for bad in (
            {"node_id": "n", "name": "<占位姓名>"},
            {"phone": "<占位电话>"},
            {"id_no": "<占位证件号>"},
            {"contact": "<占位联系方式>"},
            {"inpatient_no": "RAW-1234567890"},
        ):
            with pytest.raises(ValueError):
                obs.assert_log_redacted(bad)

    def test_accepts_clean_dict_and_json_lines(self):
        assert obs.assert_log_redacted({"node_id": "n", "inpatient_digest": "ab12cd34ef56"}) is True
        lines = "\n".join([
            json.dumps({"node_id": "a", "status": "ok"}),
            json.dumps({"node_id": "b", "error_code": "FIELD_MISSING"}),
        ])
        assert obs.assert_log_redacted(lines) is True


class TestLogFileScan:
    def test_scan_log_file_finds_no_phi(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ITC_LOG_DIR", str(tmp_path))
        for i in range(6):
            obs.structured_log({
                "run_id": "BATCH", "node_id": f"n{i}", "node_type": "rule_judge",
                "status": "ok", "duration_ms": 1.0 * i,
                "error_code": "" if i % 2 else "IDEMPOTENCY_HIT",
                "inpatient_no": f"INP-{i}",
            })
        scanned = obs.scan_log_file()
        assert scanned >= 6
        for raw in (tmp_path / "structured.jsonl").read_text(encoding="utf-8").splitlines():
            assert obs.assert_log_redacted(raw) is True
            d = json.loads(raw)
            for forbidden in ("name", "id_no", "phone", "contact", "address", "inpatient_no"):
                assert forbidden not in d


class TestBatchSummary:
    def test_write_batch_summary_with_db_conn(self, db_conn, tmp_path):
        stats = {
            "processed": 10, "cache_hits": 3, "blocked": 1,
            "manual": 2, "writebacks": 4, "failed": 0,
            "extra_metric": 99,
        }
        path = obs.write_batch_summary(db_conn, stats, path=tmp_path / "batch.json")
        assert isinstance(path, Path) and path.is_file()
        d = json.loads(path.read_text(encoding="utf-8"))
        for key in ("processed", "cache_hits", "blocked", "manual", "writebacks", "failed"):
            assert key in d
        assert d["processed"] == 10
        assert d["cache_hits"] == 3
        assert d["extra_metric"] == 99  # 额外统计原样保留
        assert "field_labels" in d
        # 同一份摘要也落到状态库独立表（最佳努力，本用例应成功）
        count = db_conn.execute("SELECT count(*) FROM itc_batch_summaries").fetchone()[0]
        assert count >= 1

    def test_write_batch_summary_fills_missing_counters(self, tmp_path):
        path = obs.write_batch_summary(None, {"processed": 5}, path=tmp_path / "s.json")
        d = json.loads(path.read_text(encoding="utf-8"))
        assert d["processed"] == 5
        for key in ("cache_hits", "blocked", "manual", "writebacks", "failed"):
            assert d[key] == 0

    def test_default_log_path_under_project_logs(self, project_root, monkeypatch):
        monkeypatch.delenv("ITC_LOG_DIR", raising=False)
        p = obs.log_path()
        assert p.parent.name == "logs"
        assert p.name == "structured.jsonl"
        assert str(p).startswith(str(project_root))
