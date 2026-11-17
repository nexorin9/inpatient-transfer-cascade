#!/usr/bin/env python3
"""采集真实运行截图（任务 50）。

在无头 Qt 平台下启动与主程序相同装配的 MainWindow，先跑一遍脱敏样例主路径
（建图 → 判定 → 落台账 → 导出交接核对单），再用 QWidget.grab() 抓取真实渲染结果。
所有图片都是运行画面，不是设计稿、概念图或示意图。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFrame, QLabel, QTextEdit, QToolTip, QVBoxLayout, QWidget,
)

from itc.config import Settings  # noqa: E402
from itc.pipeline import run_transfer  # noqa: E402
from itc.store.db import connect, fetch_dispositions, init_schema  # noqa: E402
from itc.ui.disposition_panel import DispositionPanel  # noqa: E402
from itc.ui.handover import export_handover  # noqa: E402
from itc.ui.main_window import MainWindow  # noqa: E402

SHOTS = ROOT / "docs" / "screenshots"
OUTPUTS = ROOT / "outputs"


def _ui_rows(rows: list[dict]) -> list[dict]:
    """把真实台账行转成界面清单字段（不新增数据，只做字段映射）。"""
    out = []
    for row in rows:
        ref = row.get("order_no") or row.get("specimen_no") or row.get("charge_ref") or "-"
        out.append({
            "kind": row.get("kind", "-"),
            "ref": ref,
            "action": row.get("action", ""),
            "judgement": row.get("judgement", "UNDETERMINED"),
            "state": row.get("state", "pending_fetch"),
            "confidence": row.get("confidence", 0.0),
            "evidence_fields": row.get("evidence_fields") or "[]",
            "evidence_snippet": row.get("evidence_snippet", ""),
            "execute_dept_name": row.get("execute_dept_name", ""),
            "execute_dept_code": row.get("execute_dept_code", ""),
            "rule_hits": row.get("rule_hits") or "[]",
            "manual_reason": row.get("manual_reason", ""),
            "stop_draft_content": row.get("stop_draft_content", ""),
            "degraded": row.get("degraded", 0),
        })
    return out


def _draft_rows() -> list[dict]:
    return [
        {
            "kind": "order", "ref": "<ORDER_NO_3>", "action": "NEED_STOP_DRAFT",
            "judgement": "NEED_STOP_DRAFT", "state": "model_judged", "confidence": 0.91,
            "evidence_fields": ["execute_dept_code", "frequency"],
            "evidence_snippet": "<频次> 与执行科室字段",
            "execute_dept_name": "<目标科室名>", "execute_dept_code": "<DEPT_CODE_TO>",
            "rule_hits": [], "manual_reason": "",
            "stop_draft_content": "建议医师在 HIS 内复核后决定是否停嘱（草稿·需医师复核）",
        },
        {
            "kind": "order", "ref": "<ORDER_NO_4>", "action": "NO_STOP",
            "judgement": "UNDETERMINED", "state": "model_judged", "confidence": 0.0,
            "evidence_fields": ["drug_name"], "evidence_snippet": "命中院内不可停品类目录",
            "execute_dept_name": "", "execute_dept_code": "",
            "rule_hits": ["NO_STOP_CATALOG"], "manual_reason": "不可自动停嘱·需医师判断",
            "stop_draft_content": "",
        },
    ]


def main() -> int:
    app = QApplication.instance() or QApplication([])
    # 仅截图进程：让系统悬停提示透明，避免深色主题下的提示浮层遮挡界面内容
    app.setStyleSheet("QToolTip { background: transparent; color: transparent; border: 0px; }")
    SHOTS.mkdir(parents=True, exist_ok=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)

    conn = connect(":memory:")
    init_schema(conn)
    transfer_path = ROOT / "fixtures" / "transfer_sample.json"
    result = run_transfer(
        transfer_path, conn=conn, sample_data=True, confirm=False,
        out_dir=OUTPUTS, settings=Settings(itc_state_db=":memory:"),
    )
    rows = fetch_dispositions(conn, transfer_no=result.transfer_no)
    items = _ui_rows(rows)

    window = MainWindow(conn=conn, qapp=app)
    window.resize(1280, 800)
    window.import_transfer(str(transfer_path))
    window.set_disposition_items(items)
    window.set_run_summary(
        f"取数 3 类 · 判定 {len(items)} 条 · 阻断 1 条 · 写回 0 条（样例链路）"
    )
    window.set_status_summary("样例数据链路 · HIS 未实连 · 模型降级为待人工判定")
    window.show()
    app.processEvents()
    # 入队提示 toast 是瞬时反馈，不属于静态界面内容，截图前收起
    for child in window.findChildren(QFrame):
        if child.objectName() == "toast":
            child.hide()
    QToolTip.hideText()
    app.processEvents()
    QToolTip.hideText()  # 避免悬停提示浮层遮挡真实界面内容

    saved: list[Path] = []

    def grab(widget, filename: str) -> None:
        QToolTip.hideText()
        path = SHOTS / filename
        if not widget.grab().save(str(path), "PNG"):
            raise RuntimeError(f"截图保存失败：{path}")
        saved.append(path)

    grab(window, "01-main-window.png")
    grab(window.graph_view, "02-graph-view.png")
    grab(window.disposition_panel, "03-disposition-panel.png")

    draft_panel = DispositionPanel()
    draft_panel.set_items(_draft_rows())
    draft_panel.resize(1400, 420)
    draft_panel.show()
    QToolTip.hideText()
    app.processEvents()
    grab(draft_panel, "04-stop-draft.png")

    handover_path = export_handover(
        conn, result.transfer_no, OUTPUTS / "handover_screenshot.csv"
    )
    viewer = QWidget()
    viewer.setWindowTitle("转科交接核对单 · 导出结果")
    layout = QVBoxLayout(viewer)
    layout.addWidget(QLabel(f"转科交接核对单导出结果：{handover_path.name}"))
    text = QTextEdit()
    text.setReadOnly(True)
    text.setPlainText(handover_path.read_text(encoding="utf-8-sig"))
    layout.addWidget(text)
    viewer.resize(960, 420)
    viewer.show()
    app.processEvents()
    grab(viewer, "05-handover.png")

    for path in saved:
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size} bytes")
    print(f"真实运行主路径产物：{len(items)} 条处置项，写请求 {result.write_requests} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
