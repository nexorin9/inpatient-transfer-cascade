"""任务 47 · 主干端到端验收：转科单 → 处置清单 → 写回 HIS → 交接核对单。

测试层注入 httpx.MockTransport 协议替身；产品代码无 mock 开关、默认路径指向真实 HIS。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from itc.config import Settings
from itc.graph.idempotency import idempotency_key
from itc.his.client import HisClient
from itc.his.reader import HisReader
from itc.his.writer import HisWriter
from itc.model_gateway.gateway import ModelGateway
from itc.pipeline import run_transfer
from itc.store.writeback_registry import WritebackRegistry

INPATIENT = "10000001"
FROM = "<DEPT_CODE_FROM>"
TO = "<DEPT_CODE_TO>"

TRANSFER = {
    "transfer_no": "<TRANSFER_NO>",
    "inpatient_no": INPATIENT,
    "from_dept_code": FROM,
    "to_dept_code": TO,
    "from_dept_name": "<原科室名>",
    "to_dept_name": "<目标科室名>",
    "ward_code": "<WARD_CODE>",
    "transfer_at": "2026-01-01T09:00:00+08:00",
}


def _order(no, exec_code, suffix):
    return {
        "order_no": no,
        "content": f"<医嘱内容 {suffix}>",
        "drug_name": f"<药品名 {suffix}>",
        "route": "<给药途径>",
        "frequency": "<频次>",
        "execute_dept_code": exec_code,
        "start_at": "2026-01-01T00:00:00+08:00",
    }


ORDERS = [
    _order("<ORDER_NO_1>", FROM, 1),
    _order("<ORDER_NO_2>", TO, 2),
    _order("<ORDER_NO_3>", FROM, 3),
]
SPECIMENS = [{
    "specimen_no": "<SPECIMEN_NO_1>",
    "test_name": "<检验项目名 1>",
    "collect_dept_code": FROM,
    "status": "pending",
}]
CHARGES = [{
    "category": "bed",
    "fee_date": "2026-01-01",
    "amount": "45.00",
    "dept_code": FROM,
}]


@pytest.fixture
def transfer_file(tmp_path):
    path = tmp_path / "transfer.json"
    path.write_text(json.dumps(TRANSFER, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def his_stack(db_conn):
    counters = {"execution_site": 0, "drafts": 0, "reads": 0, "keys": []}

    def handler(request):
        path = request.url.path
        params = request.url.params
        if path.endswith("/orders") and params.get("status") == "executing":
            counters["reads"] += 1
            return httpx.Response(200, json={"items": ORDERS})
        if path.endswith("/specimens") and params.get("status") == "pending":
            counters["reads"] += 1
            return httpx.Response(200, json={"items": SPECIMENS})
        if path.endswith("/charges") and params.get("category") == "bed":
            counters["reads"] += 1
            return httpx.Response(200, json={"items": CHARGES})
        if path.endswith("/execution-site") and request.method == "POST":
            counters["execution_site"] += 1
            counters["keys"].append(request.headers.get("Idempotency-Key", ""))
            return httpx.Response(200, json={"ok": True})
        if path == "/api/v1/orders/drafts" and request.method == "POST":
            counters["drafts"] += 1
            return httpx.Response(200, json={"ok": True, "status": "DRAFT"})
        return httpx.Response(404, json={"error": "unknown path"})

    client = HisClient(
        base_url="https://his.example.local", token="test-token",
        transport=httpx.MockTransport(handler),
    )
    return HisReader(client), HisWriter(
        client, registry=WritebackRegistry(db_conn), conn=db_conn
    ), counters


@pytest.fixture
def llm_gateway():
    def handler(request):
        text = request.content.decode("utf-8", errors="ignore")
        if "<ORDER_NO_2>" in text:
            no, judgement = "<ORDER_NO_2>", "KEEP"
        elif "<ORDER_NO_3>" in text:
            no, judgement = "<ORDER_NO_3>", "NEED_STOP_DRAFT"
        else:
            no, judgement = "<ORDER_NO_1>", "CHANGE_SITE"
        payload = {
            "order_no": no,
            "judgement": judgement,
            "confidence": 0.91,
            "evidence_fields": ["execute_dept_code", "frequency"],
            "evidence_snippet": "<频次> 与执行科室字段",
            "reasoning_summary": "转科后执行地点需随新病区变更" if judgement == "CHANGE_SITE" else "规则可判",
            "stop_draft_content": "建议医师复核是否停嘱" if judgement == "NEED_STOP_DRAFT" else "",
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]
        })

    cfg = Settings(
        llm_base_url="https://llm.example.local/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_timeout_seconds=5.0,
    )
    return ModelGateway(settings=cfg, transport=httpx.MockTransport(handler)), cfg


def _run(transfer_file, db_conn, his_stack, llm_gateway, out_dir, *, confirm):
    reader, writer, _ = his_stack
    gateway, cfg = llm_gateway
    return run_transfer(
        transfer_file, conn=db_conn, reader=reader, writer=writer, gateway=gateway,
        confirm=confirm, out_dir=out_dir, settings=cfg,
    )


def test_three_artifacts_and_no_write_without_confirmation(
    transfer_file, db_conn, his_stack, llm_gateway, tmp_path
):
    out = tmp_path / "out1"
    result = _run(transfer_file, db_conn, his_stack, llm_gateway, out, confirm=False)
    _, _, counters = his_stack

    assert (out / "scope_list.json").exists()
    assert (out / "disposition_list.json").exists()
    assert (out / "handover.csv").exists()
    scope = json.loads((out / "scope_list.json").read_text(encoding="utf-8"))
    assert "transfer_no" in scope and "items" in scope and len(scope["items"]) >= 5
    disposition = json.loads((out / "disposition_list.json").read_text(encoding="utf-8"))
    assert len(disposition["items"]) >= 5
    assert result.write_requests == 0 and result.draft_requests == 0
    assert counters["execution_site"] == 0 and counters["drafts"] == 0


def test_confirm_writes_each_item_once_with_idempotency_key(
    transfer_file, db_conn, his_stack, llm_gateway, tmp_path
):
    out = tmp_path / "out2"
    result = _run(transfer_file, db_conn, his_stack, llm_gateway, out, confirm=True)
    _, _, counters = his_stack

    assert result.write_requests == 1, "仅 CHANGE_SITE 项发写请求"
    assert counters["execution_site"] == 1
    assert len(result.idempotency_keys) == 1
    expected = idempotency_key(
        TRANSFER["transfer_no"], "<ORDER_NO_1>", TRANSFER["to_dept_code"], TRANSFER["transfer_at"]
    )
    assert result.idempotency_keys[0] == expected
    assert counters["keys"] == [expected]
    assert result.draft_requests == 1, "停嘱只到 DRAFT 草稿接口"


def test_repeated_submission_does_not_write_twice(
    transfer_file, db_conn, his_stack, llm_gateway, tmp_path
):
    _run(transfer_file, db_conn, his_stack, llm_gateway, tmp_path / "out3a", confirm=True)
    second = _run(transfer_file, db_conn, his_stack, llm_gateway, tmp_path / "out3b", confirm=True)
    _, _, counters = his_stack

    assert counters["execution_site"] == 1, "MockTransport 仅收到 1 次 execution-site POST"
    assert second.write_requests == 0


def test_stop_draft_is_draft_only_and_no_submit_path(
    transfer_file, db_conn, his_stack, llm_gateway, tmp_path, project_root
):
    out = tmp_path / "out4"
    _run(transfer_file, db_conn, his_stack, llm_gateway, out, confirm=True)
    drafts = json.loads((out / "stop_drafts.json").read_text(encoding="utf-8"))
    assert drafts["label"] == "草稿·需医师复核"
    assert drafts["drafts"] and all(d["status"] == "DRAFT" for d in drafts["drafts"])
    assert all("草稿·需医师复核" in d["label"] for d in drafts["drafts"])

    for rel in ("src/itc/his/writer.py", "src/itc/pipeline.py", "src/itc/__main__.py"):
        source = (project_root / rel).read_text(encoding="utf-8")
        assert "submit_order(" not in source
        assert "sign_order(" not in source
        assert ".submit(" not in source
        assert ".sign(" not in source


def test_degraded_sample_chain_never_writes(
    transfer_file, db_conn, his_stack, tmp_path
):
    reader, writer, counters = his_stack
    out = tmp_path / "out5"
    result = run_transfer(
        transfer_file, conn=db_conn, reader=reader, writer=writer, gateway=None,
        confirm=True, out_dir=out, sample_data=False,
    )
    assert result.degraded is True
    assert result.write_requests == 0 and result.draft_requests == 0
    assert counters["execution_site"] == 0 and counters["drafts"] == 0
    rows = json.loads((out / "disposition_list.json").read_text(encoding="utf-8"))["items"]
    assert all(row["state"] != "awaiting_confirm" for row in rows), "语义项不得自动推进"


def test_readme_quickstart_command_produces_three_artifacts(project_root, tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")
    out = tmp_path / "cliout"
    proc = subprocess.run(
        [sys.executable, "-m", "itc", "run", "--transfer", "fixtures/transfer_sample.json",
         "--sample-data", "--out", str(out), "--db", str(tmp_path / "cli.sqlite3")],
        cwd=str(project_root), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (out / "scope_list.json").exists()
    assert (out / "disposition_list.json").exists()
    assert (out / "handover.csv").exists()
    assert '"write_requests": 0' in proc.stdout
    scope = json.loads((out / "scope_list.json").read_text(encoding="utf-8"))
    assert "transfer_no" in scope and "items" in scope


def test_doctor_reports_missing_credentials_explicitly(project_root):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")
    for key in ("HIS_BASE_URL", "HIS_API_TOKEN", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, "-m", "itc", "doctor"], cwd=str(project_root),
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "HIS 出站未配置" in proc.stdout
    assert "未配置" in proc.stdout
