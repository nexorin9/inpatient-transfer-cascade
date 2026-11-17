"""任务 44（交接单部分）：脱敏可打印表格、条目数与台账一致、状态区分。"""
from __future__ import annotations

import csv
import io

import pytest

from itc.domain.models import DispositionItem
from itc.store.db import insert_disposition
from itc.store.ledger import digest_inpatient_no, query_ledger
from itc.ui import handover


def _seed(conn, transfer_no: str = "T-1") -> None:
    specs = [
        ("O0", "written_back", "CHANGE_SITE", 0.95, ""),
        ("O1", "awaiting_confirm", "CHANGE_SITE", 0.90, ""),
        ("O2", "model_judged", "UNDETERMINED", 0.0, "规则歧义"),
        ("O3", "failed", "CHANGE_SITE", 0.80, "写回失败"),
    ]
    for ref, state, judgement, confidence, reason in specs:
        insert_disposition(conn, DispositionItem(
            transfer_no=transfer_no, inpatient_no="001234567",
            kind="order", ref=ref, judgement=judgement, state=state,
            confidence=confidence, evidence_snippet="执行科室不一致",
            reasoning_summary="规则与模型判定摘要", manual_reason=reason,
            execute_dept_name="检验科", execute_dept_code="LAB",
            idempotency_key=f"key-{ref}",
        ))


def test_csv_row_count_matches_ledger(qapp, db_conn, tmp_path):
    _seed(db_conn)
    ledger_rows = query_ledger(db_conn, transfer_no="T-1")
    out = handover.export_handover(db_conn, "T-1", tmp_path / "handover.csv")
    assert out.is_file()
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(handover.HANDOVER_COLUMNS)
    assert len(rows) - 1 == len(ledger_rows) == 4
    assert len(handover.handover_rows(db_conn, "T-1")) == len(ledger_rows)


def test_status_column_distinguishes_written_awaiting_manual(qapp, db_conn, tmp_path):
    _seed(db_conn)
    out = handover.export_handover(db_conn, "T-1", tmp_path / "h.csv")
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    statuses = [r["状态"] for r in rows]
    assert "已回写" in statuses
    assert "待确认" in statuses
    assert "待人工" in statuses
    assert statuses[0] == "已回写"
    assert statuses[3] == "失败·待重写"


def test_no_phi_in_export(qapp, db_conn, tmp_path):
    _seed(db_conn)
    # 额外写入含敏感键的条目，验证白名单字段挑选不会把它们带出来
    insert_disposition(db_conn, DispositionItem(
        transfer_no="T-1", inpatient_no="001234567", kind="order", ref="O-PHI",
        judgement="KEEP", state="awaiting_confirm", confidence=0.5,
        evidence_snippet="执行科室不一致",
    ))
    db_conn.execute(
        "UPDATE dispositions SET reasoning_summary=? WHERE order_no=?",
        ("患者姓名 张三 身份证 X123 联系方式 13800000000", "O-PHI"),
    )
    db_conn.commit()
    out = handover.export_handover(db_conn, "T-1", tmp_path / "h.csv")
    text = out.read_text(encoding="utf-8-sig")
    assert "张三" not in text
    assert "X123" not in text
    assert "13800000000" not in text
    assert "姓名" not in text and "身份证" not in text and "联系方式" not in text
    assert not handover.contains_phi(text)
    assert "001234567" not in text  # 住院号原文不落文件
    assert digest_inpatient_no("001234567") in text  # 只留摘要


def test_html_table_is_printable_and_matches_count(qapp, db_conn, tmp_path):
    _seed(db_conn)
    out = handover.export_handover(db_conn, "T-1", tmp_path / "h.html")
    html = out.read_text(encoding="utf-8")
    assert "<table" in html and "<thead>" in html and "<tbody>" in html
    assert "转科交接核对单 · T-1" in html
    assert html.count('class="row"') == 4
    assert "@media print" in html
    assert not handover.contains_phi(html)


def test_injected_items_do_not_need_db(qapp, tmp_path):
    items = [
        {"transfer_no": "T-9", "inpatient_no": "888", "kind": "specimen",
         "ref": "S1", "state": "written_back", "judgement": "KEEP",
         "confidence": 0.6, "evidence_snippet": "标本去向变更",
         "name": "张三", "id_no": "ID-1", "phone": "13900000000"},
        {"transfer_no": "T-9", "inpatient_no": "888", "kind": "charge",
         "ref": "C1", "state": "awaiting_confirm", "judgement": "KEEP",
         "confidence": 0.4},
    ]
    out = handover.export_handover(None, "T-9", tmp_path / "inj.csv", items=items)
    text = out.read_text(encoding="utf-8-sig")
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(items) == 2
    assert "张三" not in text and "ID-1" not in text and "13900000000" not in text
    assert "888" not in text
    assert rows[0]["业务对象"] == "specimen · S1"
    assert rows[0]["状态"] == "已回写"


def test_handover_status_mapping():
    assert handover.handover_status({"state": "written_back"}) == "已回写"
    assert handover.handover_status({"state": "failed"}) == "失败·待重写"
    assert handover.handover_status({"state": "blocked"}) == "阻断"
    assert handover.handover_status({"state": "awaiting_confirm"}) == "待确认"
    assert handover.handover_status({"state": "model_judged", "degraded": True}) == "待人工"
    assert handover.handover_status({"state": "model_judged",
                                     "judgement": "NEED_STOP_DRAFT"}) == "待人工"
    assert handover.handover_status({"state": "model_judged",
                                     "rule_hits": ["no_stop"]}) == "待人工"
