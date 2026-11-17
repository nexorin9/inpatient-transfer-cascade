"""任务 41：联动图节点卡、连线、阻断标记、进度着色与点击联动。"""
from __future__ import annotations

import pytest

from itc.graph.events import ProgressEvent
from itc.graph.model import Graph, Link, Node
from itc.ui import tokens
from itc.ui.graph_view import GraphView, NodeCardItem, normalize_graph


@pytest.fixture
def chain_graph():
    return {
        "nodes": [
            {"id": "transfer", "class_type": "transfer", "title": "转科单"},
            {"id": "fetch", "class_type": "fetch_orders", "title": "取数"},
            {"id": "rule", "class_type": "rule_judge", "title": "规则判定"},
            {"id": "model", "class_type": "model_judge", "title": "模型判定"},
            {"id": "confirm", "class_type": "confirm_writeback", "title": "确认回写"},
        ],
        "links": [
            {"from": "transfer", "to": "fetch"}, {"from": "fetch", "to": "rule"},
            {"from": "rule", "to": "model"}, {"from": "model", "to": "confirm"},
        ],
    }


def test_render_node_count_matches_graph(qapp, chain_graph):
    view = GraphView()
    ids = view.set_graph(chain_graph)
    assert ids == [n["id"] for n in chain_graph["nodes"]]
    assert view.node_count() == len(chain_graph["nodes"]) == 5
    assert len(view.node_items()) == len(ids)
    assert all(isinstance(item, NodeCardItem) for item in view.node_items())
    assert len(view.link_items()) == len(chain_graph["links"]) == 4


def test_node_cards_carry_type_title_and_status(qapp, chain_graph):
    view = GraphView()
    view.set_graph(chain_graph, statuses={"fetch": "running"})
    card = view.card_for("fetch")
    assert card is not None
    assert card.class_type == "fetch_orders"
    assert card.title == "取数"
    assert card.status == "running"
    assert view.statuses()["fetch"] == "running"
    assert all(c.status == "pending" for c in view.node_items() if c.node_id != "fetch")


def test_g1_graph_object_is_supported(qapp):
    graph = Graph()
    for nid, ctype in (("n1", "fetch_orders"), ("n2", "rule_judge"), ("n3", "stop_draft")):
        graph.add_node(Node(nid, ctype))
    graph.add_link(Link("n1", 0, "n2", "in"))
    graph.add_link(Link("n2", 0, "n3", "in"))
    view = GraphView()
    assert view.set_graph(graph) == graph.node_ids()
    assert view.node_count() == len(graph.node_ids()) == 3
    assert len(view.link_items()) == 2
    # to_dict() 形态同样可渲染
    assert view.set_graph(graph.to_dict()) == graph.node_ids()
    assert len(view.link_items()) == 2


def test_normalize_graph_variants():
    nodes, links = normalize_graph([{"id": "a", "title": "A"}, {"id": "b", "title": "B"}])
    assert [n["id"] for n in nodes] == ["a", "b"]
    assert links == []
    empty_nodes, empty_links = normalize_graph(None)
    assert empty_nodes == [] and empty_links == []


def test_five_visual_status_classes(qapp, chain_graph):
    view = GraphView()
    view.set_graph(chain_graph)
    view.set_statuses({
        "transfer": "pending", "fetch": "running", "rule": "success",
        "model": "manual", "confirm": "failed",
    })
    colors = [tokens.node_status_color(c.status) for c in view.node_items()]
    assert len(set(colors)) == 5
    assert tokens.node_status_color("pending") == tokens.COLORS["neutral_disabled"]
    assert tokens.node_status_color("running") == tokens.COLORS["primary"]
    assert tokens.node_status_color("success") == tokens.COLORS["success"]
    assert tokens.node_status_color("manual") == tokens.COLORS["warning"]
    assert tokens.node_status_color("failed") == tokens.COLORS["danger"]


def test_blocked_marker(qapp, chain_graph):
    view = GraphView()
    view.set_graph(chain_graph)
    view.update_progress(ProgressEvent(run_id="r", node_id="rule",
                                       node_type="rule_judge", status="blocked",
                                       message="规则拒绝自动处置"))
    card = view.card_for("rule")
    assert card.status == "blocked"
    assert "rule" in view.blocked_markers()
    assert "规则拒绝" in card.toolTip()


def test_update_progress_accepts_event_and_dict(qapp, chain_graph):
    view = GraphView()
    view.set_graph(chain_graph)
    view.update_progress(ProgressEvent(run_id="r", node_id="model",
                                       node_type="model_judge", status="running"))
    assert view.statuses()["model"] == "running"
    view.update_progress({"node_id": "model", "status": "success"})
    assert view.statuses()["model"] == "success"
    view.update_progress({"node_id": "confirm", "status": "待人工"})
    assert view.statuses()["confirm"] == "manual"


def test_click_node_triggers_callback_and_signal(qapp, chain_graph):
    view = GraphView()
    view.set_graph(chain_graph)
    seen: list[str] = []
    signals: list[str] = []
    view.set_node_clicked_callback(lambda node_id: seen.append(node_id))
    view.nodeClicked.connect(lambda node_id: signals.append(node_id))
    view.select_node("rule")
    assert seen == ["rule"]
    assert signals == ["rule"]
    assert view.card_for("rule").isSelected() is True
    assert view.card_for("transfer").isSelected() is False


def test_clear_resets_scene(qapp, chain_graph):
    view = GraphView()
    view.set_graph(chain_graph)
    assert view.node_count() == 5
    view.clear()
    assert view.node_items() == []
    assert view.link_items() == []
    assert view.node_ids() == []
