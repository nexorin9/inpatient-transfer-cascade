"""任务 40 / 45（部分）：主窗口骨架、导入 / 拉取、建图与进度事件、三态。"""
from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QLabel, QMainWindow, QPushButton

from itc.graph.events import EventBus, ProgressEvent
from itc.ui import states
from itc.ui.main_window import DEFAULT_GRAPH_NODES, MainWindow, build_transfer_graph


@pytest.fixture
def valid_transfer(fixtures_dir):
    return json.loads((fixtures_dir / "transfer_sample.json").read_text(encoding="utf-8"))


def test_smoke_instantiation(qapp):
    """冒烟：无头环境下实例化主窗口不抛异常，不调用事件循环。"""
    window = MainWindow(conn=None, qapp=qapp)
    assert isinstance(window, QMainWindow)
    assert window.acceptDrops() is True
    assert window.centralWidget() is not None


def test_backend_layout_header_and_content(qapp):
    window = MainWindow(qapp=qapp)
    assert tuple(window.nav_buttons) == ("转科处置", "待确认清单", "待重写队列", "交接核对单")
    pull = window.findChild(QPushButton, "pullTodayButton")
    assert pull is not None and pull.text() == "拉取今日转科"
    assert window.findChild(QLabel, "pageTitle").text() == "住院转科连带处置"
    assert window.findChild(QLabel, "helperText") is not None
    assert window.paste_edit is not None
    assert window.content_stack.count() == 2  # 三态页 + 工作区页


def test_three_states_have_distinct_renders(qapp):
    window = MainWindow(qapp=qapp)
    rendered = {}
    for state in ("loading", "empty", "error"):
        assert window.show_state(state) == state
        assert window.placeholder.state() == state
        assert window.content_stack.currentIndex() == 0
        rendered[state] = (window.placeholder.title_label.text(),
                           window.placeholder.detail_label.text())
    assert len({text for text, _ in rendered.values()}) == 3
    assert rendered["loading"][0] == states.LOADING_COPY["fetch"]
    assert rendered["error"][0] == states.ERROR_COPY["his"][0]
    assert window.show_state("ready") == "ready"
    assert window.content_stack.currentIndex() == 1


def test_import_valid_transfer_builds_graph_and_progress(qapp, valid_transfer):
    bus = EventBus()
    window = MainWindow(qapp=qapp, bus=bus)
    result = window.import_transfer(json.dumps(valid_transfer))
    assert result["ok"] is True
    assert result["transfer_no"] == valid_transfer["transfer_no"]
    assert result["node_count"] == len(DEFAULT_GRAPH_NODES) == 5
    assert window.graph_view.node_count() == 5
    assert window.graph_view.node_ids() == [n["id"] for n in DEFAULT_GRAPH_NODES]
    statuses = window.graph_view.statuses()
    assert statuses["transfer"] == "success"      # 入队完成
    assert statuses["fetch"] == "running"          # 取数进行中
    assert "入队" in window.run_summary_text() or "进度" in window.run_summary_text()


def test_import_from_file_and_paste_text(qapp, valid_transfer, tmp_path):
    window = MainWindow(qapp=qapp)
    path = tmp_path / "transfer.json"
    path.write_text(json.dumps(valid_transfer), encoding="utf-8")
    assert window.import_transfer(str(path))["ok"] is True
    assert window.import_transfer(path)["ok"] is True
    window.paste_edit.setPlainText(json.dumps(valid_transfer))
    assert window.paste_json(window.paste_edit.toPlainText())["ok"] is True
    assert window._on_paste_clicked()["ok"] is True
    assert window.last_drop_result == {}  # 未走拖放事件时保持空


def test_invalid_fields_give_row_level_hints(qapp):
    window = MainWindow(qapp=qapp)
    result = window.import_transfer('{"transfer_no": "T-1"}')
    assert result["ok"] is False
    assert result["node_count"] == 0
    assert result["errors"]
    assert result["errors"][0].startswith("第 1 行")
    assert "缺少必填字段" in result["errors"][0]
    assert window.validation_errors() == result["errors"]
    assert window.import_errors_label.text() == "\n".join(result["errors"])


def test_multi_row_import_reports_each_bad_row(qapp, valid_transfer):
    window = MainWindow(qapp=qapp)
    payload = [valid_transfer, {"transfer_no": "T-2"}, {"inpatient_no": "1"}]
    result = window.import_transfer(payload)
    assert result["ok"] is False
    assert len(result["errors"]) == 2
    assert result["errors"][0].startswith("第 2 行")
    assert result["errors"][1].startswith("第 3 行")


def test_bad_json_is_reported(qapp):
    window = MainWindow(qapp=qapp)
    result = window.import_transfer("{not-json")
    assert result["ok"] is False
    assert "JSON 解析失败" in result["errors"][0]


def test_pull_today_without_loader_shows_unreachable_hint(qapp):
    states.clear_toast_history()
    window = MainWindow(qapp=qapp)
    result = window.request_today_transfers()
    assert result["ok"] is False
    assert result["reason"] == "his_unreachable"
    assert any("HIS 未连通" in t for t in states.toast_history())


def test_pull_today_with_injected_loader(qapp, valid_transfer):
    window = MainWindow(qapp=qapp)
    window.today_loader = lambda: [valid_transfer]
    result = window.request_today_transfers()
    assert result["ok"] is True
    assert len(window._today_rows) == 1
    assert window.graph_view.node_count() == 5
    assert window.select_today_transfer(0)["ok"] is True
    assert window.select_today_transfer(9)["ok"] is False


def test_load_today_transfers_row_level_hints(qapp, valid_transfer):
    window = MainWindow(qapp=qapp)
    result = window.load_today_transfers([valid_transfer, {"transfer_no": "bad"}])
    assert result["ok"] is True
    assert len(window.last_import_errors) == 1
    assert "第 2 行" in window.last_import_errors[0]
    assert window.graph_view.node_count() == 5


def test_progress_events_are_consumed(qapp, valid_transfer):
    bus = EventBus()
    window = MainWindow(qapp=qapp, bus=bus)
    window.import_transfer(valid_transfer)
    bus.emit(ProgressEvent(run_id="r", node_id="rule", node_type="rule_judge",
                           status="blocked", message="规则拒绝自动处置"))
    assert window.graph_view.statuses()["rule"] == "blocked"
    assert "rule" in window.graph_view.blocked_markers()
    assert "blocked" in window.run_summary_text()
    window.on_progress({"node_id": "model", "status": "success"})
    assert window.graph_view.statuses()["model"] == "success"


def test_node_click_links_to_disposition_list(qapp, valid_transfer):
    window = MainWindow(qapp=qapp)
    window.import_transfer(valid_transfer)
    seen: list[str] = []
    window.on_node_clicked = lambda node_id: seen.append(node_id)
    window.set_disposition_items([{
        "transfer_no": "T", "inpatient_no": "1", "kind": "order", "ref": "O1",
        "judgement": "CHANGE_SITE", "state": "awaiting_confirm", "confidence": 0.9,
    }])
    window.graph_view.select_node("model")
    assert seen == ["model"]
    assert window.last_clicked_node == "model"
    assert window.disposition_panel.table_view.model().rowCount() == 1


def test_export_handover_with_injected_items(qapp, valid_transfer, tmp_path):
    window = MainWindow(qapp=qapp)
    window.import_transfer(valid_transfer)
    items = [
        {"transfer_no": "T", "inpatient_no": "001", "kind": "order", "ref": "O1",
         "state": "written_back", "judgement": "CHANGE_SITE", "confidence": 0.9},
        {"transfer_no": "T", "inpatient_no": "001", "kind": "order", "ref": "O2",
         "state": "awaiting_confirm", "judgement": "KEEP", "confidence": 0.8},
    ]
    window.set_disposition_items(items)
    out = tmp_path / "handover.csv"
    result = window.export_handover(out)
    assert result == out and out.is_file()
    lines = out.read_text(encoding="utf-8-sig").strip().splitlines()
    assert len(lines) == 3  # 表头 + 2 条


def test_set_width_two_tiers(qapp):
    window = MainWindow(qapp=qapp)
    assert window.set_width(768) == "narrow"
    assert window.set_width(1280) == "wide"
    assert window.property("width_tier") == "wide"


def test_tray_menu_is_minimal_and_restore_works(qapp):
    window = MainWindow(qapp=qapp)
    assert window.tray.menu_items() == ["拉取今日转科", "待确认清单", "待重写队列", "退出"]
    before = window.restore_count
    window.tray.request_restore()
    assert window.restore_count == before + 1
    assert window.request_quit() is False  # 无头环境按安全默认拒绝破坏性操作


def test_build_transfer_graph_shape():
    graph = build_transfer_graph("T-1")
    assert len(graph["nodes"]) == 5
    assert len(graph["links"]) == 4
    assert graph["links"][0] == {"from": "transfer", "to": "fetch"}
    assert graph["nodes"][0]["title"].endswith("T-1")
