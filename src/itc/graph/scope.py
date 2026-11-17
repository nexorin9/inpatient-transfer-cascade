"""主干最窄端到端切片：读样例 -> 建图 -> 跑就绪调度 -> 产出牵动范围清单。

G1 自留的最窄切片：不依赖 HIS / 模型 / UI，只用 G1 内部图模块与标准库。
"""
from __future__ import annotations

import json
from pathlib import Path

from itc.graph.builder import GraphBuilder
from itc.graph.scheduler import ready_order

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_OUTPUTS_DIR = _PROJECT_ROOT / "outputs"


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"不支持的数据行类型：{type(value)!r}")


def _build_graph(transfer, orders, specimens, charges):
    """把四类样例装配成一张可执行的联动图，并返回节点 id -> 清单条目映射。"""
    inpatient_no = transfer["inpatient_no"]
    b = GraphBuilder(prefix="")
    b.node(
        "transfer_context",
        id="ctx",
        transfer_no=transfer["transfer_no"],
        inpatient_no=inpatient_no,
    )
    fetch_o = b.node("fetch_orders", id="fetch_orders", inpatient_no=inpatient_no)
    fetch_s = b.node("fetch_specimens", id="fetch_specimens", inpatient_no=inpatient_no)
    fetch_c = b.node("fetch_charges", id="fetch_charges", inpatient_no=inpatient_no)
    judge = b.node(
        "rule_judge",
        id="judge",
        orders=fetch_o.out(0),
        specimens=fetch_s.out(0),
        charges=fetch_c.out(0),
    )

    item_by_node = {}
    to_dept = transfer.get("to_dept_code", "")

    for order in orders:
        o = _as_dict(order)
        node_id = f"order:{o['order_no']}"
        b.node(
            "change_site",
            id=node_id,
            source=judge.out(0),
            order_no=o["order_no"],
            execute_dept_code=o.get("execute_dept_code", ""),
        )
        need_change = o.get("execute_dept_code") != to_dept
        item_by_node[node_id] = {
            "kind": "order",
            "ref": o["order_no"],
            "action": "CHANGE_SITE" if need_change else "KEEP",
            "note": (
                "执行科室仍留在原科室，转科后需变更执行地点"
                if need_change
                else "执行科室已在目标科室，无需改动"
            ),
        }

    for specimen in specimens:
        s = _as_dict(specimen)
        if s.get("status") != "pending":
            continue
        node_id = f"specimen:{s['specimen_no']}"
        b.node(
            "specimen_route",
            id=node_id,
            source=judge.out(0),
            specimen_no=s["specimen_no"],
            test_name=s.get("test_name", ""),
        )
        item_by_node[node_id] = {
            "kind": "specimen",
            "ref": s["specimen_no"],
            "action": "SPECIMEN_ROUTE",
            "note": "待送标本需确认转科后的送检去向",
        }

    for idx, charge in enumerate(charges):
        c = _as_dict(charge)
        node_id = f"charge:{idx}"
        b.node(
            "bed_fee_hint",
            id=node_id,
            source=judge.out(0),
            fee_date=c.get("fee_date", ""),
            amount=c.get("amount", ""),
        )
        mismatch = c.get("dept_code") != to_dept
        item_by_node[node_id] = {
            "kind": "charge",
            "ref": f"{c.get('fee_date', '')}|{c.get('dept_code', '')}|{c.get('amount', '')}",
            "action": "BED_FEE_HINT",
            "note": (
                "床位费归属科室与新病区不一致，需人工核对归属"
                if mismatch
                else "床位费归属与新病区一致，仅作一致性提示"
            ),
        }

    return b.build(), item_by_node


def build_scope_list(transfer: dict, orders: list, specimens: list, charges: list) -> dict:
    """读数据 -> 建图 -> 跑就绪调度 -> 产出含 transfer_no 与 items 的牵动范围清单。"""
    transfer = _as_dict(transfer)
    graph, item_by_node = _build_graph(transfer, orders, specimens, charges)
    node_order = ready_order(graph)  # 真实跑一遍就绪调度，含环会在此暴露
    items = []
    for node_id in node_order:
        if node_id in item_by_node:
            items.append(item_by_node[node_id])
    return {
        "transfer_no": transfer["transfer_no"],
        "inpatient_no": transfer["inpatient_no"],
        "items": items,
        "node_order": node_order,
    }


def write_scope_list(scope, out_path) -> Path:
    """把牵动范围清单写成 JSON；相对路径统一落到项目 outputs/ 目录。"""
    path = Path(out_path)
    if not path.is_absolute():
        path = _OUTPUTS_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scope, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
