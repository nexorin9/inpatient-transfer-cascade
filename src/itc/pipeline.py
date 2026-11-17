"""端到端主路径编排：转科单 → 牵动范围清单 → 逐条处置清单 → 写回 HIS → 交接核对单。

产品默认路径指向真实 HIS 端点（凭据从环境变量读），不设 mock 开关：
- ``reader`` / ``writer`` / ``gateway`` 是依赖注入位，测试层可注入协议替身；
- ``sample_data=True`` 是显式声明的「脱敏样例链路」，只用于最小链路自检，
  不冒充 HIS 实连，也不发任何出站写请求。

停嘱只到 DRAFT：本模块不提供任何提交 / 签名医嘱的调用路径。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from itc.config import ConfigurationError, get_settings, require_his_credentials
from itc.domain.models import DispositionItem, TransferOrder
from itc.graph.idempotency import idempotency_key
from itc.graph.scope import build_scope_list, write_scope_list
from itc.his.client import HisClient
from itc.his.reader import HisReader
from itc.his.redact import digest_inpatient_no
from itc.his.writer import HisWriter
from itc.model_gateway.degradation import apply_degradation
from itc.rules.bed_fee import bed_fee_hints
from itc.rules.conflicts import judge_execution_site
from itc.rules.no_stop import is_no_stop
from itc.rules.specimen_route import judge_specimen_route
from itc.store.db import (
    append_audit,
    connect,
    init_schema,
    insert_disposition,
    now_iso,
    update_disposition,
)
from itc.store.state_machine import transition
from itc.store.writeback_registry import WritebackRegistry
from itc.ui.handover import export_handover

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURES = PROJECT_ROOT / "fixtures"
DEFAULT_OUTPUTS = PROJECT_ROOT / "outputs"

STOP_DRAFT_LABEL = "草稿·需医师复核"


@dataclass
class PipelineResult:
    """一次转科处置的可核对结果。"""
    transfer_no: str
    run_id: str
    source: str
    artifacts: dict = field(default_factory=dict)
    items: list = field(default_factory=list)
    scope: dict = field(default_factory=dict)
    write_requests: int = 0
    draft_requests: int = 0
    idempotency_keys: list = field(default_factory=list)
    stop_drafts: list = field(default_factory=list)
    degraded: bool = False
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "transfer_no": self.transfer_no,
            "run_id": self.run_id,
            "source": self.source,
            "artifacts": {k: str(v) for k, v in self.artifacts.items()},
            "item_count": len(self.items),
            "write_requests": self.write_requests,
            "draft_requests": self.draft_requests,
            "idempotency_keys": self.idempotency_keys,
            "stop_draft_count": len(self.stop_drafts),
            "degraded": self.degraded,
            "errors": self.errors,
        }


class _CountingWriter:
    """写回计数包装：区分「调用次数」与「真实发出的 POST 次数」（幂等命中不算）。"""
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.requests = 0
        self.draft_calls = 0
        self.draft_requests = 0

    @staticmethod
    def _sent(result) -> bool:
        if isinstance(result, dict):
            return bool(result.get("request_sent", True))
        return True

    def write_execution_site(self, **kwargs):
        self.calls += 1
        result = self.inner.write_execution_site(**kwargs)
        if self._sent(result):
            self.requests += 1
        return result

    def create_stop_draft(self, **kwargs):
        self.draft_calls += 1
        result = self.inner.create_stop_draft(**kwargs)
        if self._sent(result):
            self.draft_requests += 1
        return result


def _load_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sample_rows(fixtures_dir: Path) -> tuple[list, list, list]:
    """脱敏样例链路：读 fixtures 的三类配套样例（不使用 HIS，也不伪造 HIS 响应）。"""
    orders = _load_json(fixtures_dir / "orders_sample.json")["items"]
    specimens = _load_json(fixtures_dir / "specimens_sample.json")["items"]
    charges = _load_json(fixtures_dir / "charges_sample.json")["items"]
    return orders, specimens, charges


def _hit_fields(hit) -> list:
    return list(getattr(hit, "fields", []) or [])


def _to_dict(value) -> dict:
    return value.to_dict() if hasattr(value, "to_dict") else dict(value)


def _build_items(transfer: TransferOrder, orders, specimens, charges, gateway, conn, run_id):
    """规则守门 + 模型判定，产出逐条处置项；模型不可达时整批降级。"""
    cfg = get_settings()
    degraded = gateway is None or not getattr(getattr(gateway, "settings", cfg), "llm_ready", False)
    items: list[DispositionItem] = []

    for raw in orders:
        order = _to_dict(raw)
        hit = judge_execution_site(order, transfer)
        no_stop = is_no_stop(order)
        judgement = "UNDETERMINED"
        confidence = 0.0
        evidence: list[str] = _hit_fields(hit)
        snippet = str(getattr(hit, "reason", "") or "")
        reasoning = ""
        draft_content = ""

        if gateway is not None and not degraded:
            verdict = gateway.judge_order(order, transfer)
            judgement = str(verdict.get("judgement", "UNDETERMINED"))
            confidence = float(verdict.get("confidence", 0.0) or 0.0)
            evidence = list(verdict.get("evidence_fields") or evidence)
            snippet = str(verdict.get("evidence_snippet") or snippet)
            reasoning = str(verdict.get("reasoning_summary") or "")
            draft_content = str(verdict.get("stop_draft_content") or "")
            if verdict.get("manual") or verdict.get("degraded"):
                degraded = True
        elif gateway is None:
            judgement = "UNDETERMINED"

        manual_reason = ""
        if getattr(hit, "manual", False):
            manual_reason = str(getattr(hit, "reason", "执行科室硬矛盾，需人工判定"))
            judgement = "UNDETERMINED"
        if no_stop is not None:
            manual_reason = manual_reason or "不可自动停嘱·需医师判断"
            judgement = "UNDETERMINED"
            draft_content = ""

        action = "CHANGE_SITE" if judgement == "CHANGE_SITE" and not manual_reason else (
            "KEEP" if judgement == "KEEP" and not manual_reason else "UNDETERMINED"
        )
        if judgement == "NEED_STOP_DRAFT" and not no_stop:
            action = "NEED_STOP_DRAFT"

        items.append(DispositionItem(
            transfer_no=transfer.transfer_no,
            inpatient_no=transfer.inpatient_no,
            kind="order",
            ref=str(order["order_no"]),
            action=action,
            judgement=judgement,
            confidence=confidence,
            evidence_fields=evidence,
            evidence_snippet=snippet,
            reasoning_summary=reasoning,
            stop_draft_content=draft_content,
            execute_dept_code=transfer.to_dept_code,
            execute_dept_name=transfer.to_dept_name,
            effective_from=transfer.transfer_at,
            rule_hits=[str(getattr(hit, "rule_id", ""))] + (
                [str(getattr(no_stop, "rule_id", ""))] if no_stop else []
            ),
            manual_reason=manual_reason,
            degraded=degraded,
            degraded_reason="模型未配置或不可达，语义项转人工判定" if degraded else "",
            run_id=run_id,
        ))

    for raw in specimens:
        specimen = _to_dict(raw)
        if specimen.get("status") != "pending":
            continue
        hit = judge_specimen_route(specimen, transfer)
        manual = bool(getattr(hit, "manual", False))
        items.append(DispositionItem(
            transfer_no=transfer.transfer_no,
            inpatient_no=transfer.inpatient_no,
            kind="specimen",
            ref=str(specimen["specimen_no"]),
            action="MANUAL_TODO" if manual else "SPECIMEN_ROUTE",
            judgement="UNDETERMINED" if manual else "KEEP",
            evidence_fields=_hit_fields(hit),
            evidence_snippet=str(getattr(hit, "reason", "") or ""),
            rule_hits=[str(getattr(hit, "rule_id", ""))],
            manual_reason=str(getattr(hit, "reason", "")) if manual else "",
            degraded=degraded,
            run_id=run_id,
        ))

    for index, raw in enumerate(charges):
        charge = _to_dict(raw)
        hints = bed_fee_hints([charge], transfer)
        hit = hints[0] if hints else None
        items.append(DispositionItem(
            transfer_no=transfer.transfer_no,
            inpatient_no=transfer.inpatient_no,
            kind="charge",
            ref=f"{charge.get('fee_date','')}|{charge.get('dept_code','')}|{charge.get('amount','')}",
            action="BED_FEE_HINT",
            judgement="KEEP",
            evidence_fields=_hit_fields(hit) if hit else ["fee_date", "dept_code"],
            evidence_snippet=str(getattr(hit, "reason", "") or "床位费归属一致性提示"),
            rule_hits=[str(getattr(hit, "rule_id", ""))] if hit else [],
            manual_reason=str(getattr(hit, "reason", "")) if hit and getattr(hit, "manual", False) else "",
            degraded=degraded,
            run_id=run_id,
        ))

    if degraded and gateway is None:
        items = [DispositionItem.from_dict(x) for x in apply_degradation(
            [i.to_dict() for i in items], "模型未配置或不可达"
        )]
    return items, degraded


def _persist_items(conn, items, run_id) -> list:
    """落库并按状态机推进；人工项停在 awaiting_confirm 之前。"""
    stored = []
    for item in items:
        item_id = insert_disposition(conn, item)
        transition(conn, item_id, "scoped")
        transition(conn, item_id, "rule_judged")
        transition(conn, item_id, "model_judged")
        # 只有「改执行地点」且无人工保留原因的项才进入待确认；标本 / 床位费 / 停嘱
        # 草稿 / 保持不动 / 人工待办一律停在 awaiting_confirm 之前，避免降级态误推进。
        if item.action == "CHANGE_SITE" and not item.manual_reason:
            transition(conn, item_id, "awaiting_confirm")
        else:
            append_audit(conn, run_id=run_id, node_id=str(item_id), event_type="manual_hold",
                         payload={"reason": item.manual_reason or "无需自动写回"})
        row = conn.execute("SELECT * FROM dispositions WHERE id=?", (item_id,)).fetchone()
        stored.append(dict(row))
    return stored


def run_transfer(
    transfer_path,
    *,
    conn=None,
    reader=None,
    writer=None,
    gateway=None,
    confirm: bool = False,
    sample_data: bool = False,
    out_dir=None,
    fixtures_dir=None,
    settings=None,
    actor: str = "护士长",
) -> PipelineResult:
    """跑完一张转科单的完整闭环，产出三份可核对产物。"""
    cfg = settings or get_settings()
    own_conn = conn is None
    conn = conn or connect(cfg.state_db_path)
    init_schema(conn)
    out = Path(out_dir) if out_dir else DEFAULT_OUTPUTS
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    fixtures = Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES

    transfer = TransferOrder.from_dict(_load_json(transfer_path))
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    source = "sample-data" if sample_data else "his"
    result = PipelineResult(transfer_no=transfer.transfer_no, run_id=run_id, source=source)

    # 1) 取数：默认真实 HIS；显式样例链路只读 fixtures，不伪装成 HIS 实连
    if sample_data:
        orders, specimens, charges = _sample_rows(fixtures)
    else:
        if reader is None:
            require_his_credentials(cfg)
            reader = HisReader(HisClient())
        orders = reader.read_executing_orders(transfer.inpatient_no)
        specimens = reader.read_pending_specimens(transfer.inpatient_no)
        charges = reader.read_bed_charges(transfer.inpatient_no)

    # 2) 建图 + 就绪调度 → 牵动范围清单
    scope = build_scope_list(transfer.to_dict(), orders, specimens, charges)
    result.scope = scope
    result.artifacts["scope"] = write_scope_list(scope, out / "scope_list.json")
    append_audit(conn, run_id=run_id, node_id="fetch", event_type="run_started",
                 payload={"transfer_no": transfer.transfer_no, "source": source,
                          "scope_items": len(scope["items"])})

    # 3) 规则守门 + 模型判定（模型不可达则降级）
    if gateway is None and cfg.llm_ready:
        from itc.model_gateway.gateway import ModelGateway
        gateway = ModelGateway(settings=cfg)
    items, degraded = _build_items(transfer, orders, specimens, charges, gateway, conn, run_id)
    result.degraded = degraded
    result.items = items

    # 4) 逐条处置清单落库（脱敏产物）
    rows = _persist_items(conn, items, run_id)
    disposition_payload = {
        "transfer_no": transfer.transfer_no,
        "inpatient_digest": digest_inpatient_no(transfer.inpatient_no),
        "degraded": degraded,
        "items": [
            {
                "kind": r["kind"],
                "ref": r["order_no"] or r["specimen_no"] or r["charge_ref"],
                "action": r["action"],
                "judgement": r["judgement"],
                "state": r["state"],
                "confidence": r["confidence"],
                "evidence_fields": json.loads(r["evidence_fields"] or "[]"),
                "evidence_snippet": r["evidence_snippet"],
                "idempotency_key": r["idempotency_key"],
            }
            for r in rows
        ],
    }
    disposition_path = out / "disposition_list.json"
    disposition_path.write_text(json.dumps(disposition_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    result.artifacts["disposition"] = disposition_path

    # 5) 停嘱只到草稿：不提交、不签名；命中不可停品类不产草稿
    drafts = []
    for row in rows:
        if row["action"] == "NEED_STOP_DRAFT" and not row["manual_reason"]:
            drafts.append({
                "order_no": row["order_no"],
                "label": STOP_DRAFT_LABEL,
                "status": "DRAFT",
                "content": next(
                    (i.stop_draft_content for i in items if i.ref == row["order_no"]), ""
                ),
                "source_transfer_no": transfer.transfer_no,
            })
    (out / "stop_drafts.json").write_text(
        json.dumps({"label": STOP_DRAFT_LABEL, "drafts": drafts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result.stop_drafts = drafts
    result.artifacts["stop_drafts"] = out / "stop_drafts.json"

    # 6) 逐条确认后才写回；未确认零写请求
    if writer is None and confirm and not sample_data:
        require_his_credentials(cfg)
        writer = HisWriter(
            HisClient(), registry=WritebackRegistry(conn), conn=conn
        )
    counting = _CountingWriter(writer) if writer is not None else None
    registry = WritebackRegistry(conn)

    for row in rows:
        if row["state"] != "awaiting_confirm" or row["action"] != "CHANGE_SITE":
            continue
        if not confirm:
            continue  # 未确认：一个写请求都不发
        key = idempotency_key(
            transfer.transfer_no, row["order_no"] or "",
            row["execute_dept_code"] or transfer.to_dept_code,
            row["effective_from"] or transfer.transfer_at,
        )
        result.idempotency_keys.append(key)
        append_audit(conn, run_id=run_id, node_id=str(row["id"]), event_type="human_confirmation",
                     actor=actor, payload={"order_no": row["order_no"], "idempotency_key": key})
        if counting is None:
            continue  # 样例链路：明确不发真实出站写请求

        def do_write(row=row, key=key):
            return counting.write_execution_site(
                order_id=row["order_no"],
                inpatient_no=transfer.inpatient_no,
                order_no=row["order_no"],
                execute_dept_code=row["execute_dept_code"] or transfer.to_dept_code,
                execute_dept_name=row["execute_dept_name"] or transfer.to_dept_name,
                effective_from=row["effective_from"] or transfer.transfer_at,
                transfer_no=transfer.transfer_no,
            )

        try:
            write_result = registry.concurrent_write(key, do_write, transfer_no=transfer.transfer_no,
                                                    order_no=row["order_no"], disposition_id=row["id"])
        except Exception as exc:  # 写回失败不静默改数据：登记失败态与可读原因
            result.errors.append(f"{row['order_no']}: {exc}")
            transition(conn, row["id"], "failed")
            append_audit(conn, run_id=run_id, node_id=str(row["id"]), event_type="writeback_failed",
                         payload={"reason": str(exc)[:200], "idempotency_key": key})
            continue
        update_disposition(conn, row["id"], idempotency_key=key, confirmed_by=actor,
                           confirmed_at=now_iso())
        transition(conn, row["id"], "written_back", role=actor)
        append_audit(conn, run_id=run_id, node_id=str(row["id"]), event_type="writeback",
                     actor=actor, payload={"idempotency_key": key,
                                          "idempotent_hit": bool(
                                              isinstance(write_result, dict)
                                              and write_result.get("idempotent_hit")
                                          )})

    # 7) 停嘱草稿：降级态零请求；正常态只到 DRAFT
    if counting is not None and confirm and not degraded:
        for draft in drafts:
            try:
                counting.create_stop_draft(
                    inpatient_no=transfer.inpatient_no,
                    order_no=draft["order_no"],
                    content=draft["content"],
                    source_transfer_no=transfer.transfer_no,
                )
            except Exception as exc:
                result.errors.append(f"草稿 {draft['order_no']}: {exc}")
    if counting is not None:
        result.write_requests = counting.requests
        result.draft_requests = counting.draft_requests

    # 8) 交接核对单（条目数与台账一致）
    result.artifacts["handover"] = export_handover(
        conn, transfer.transfer_no, out / "handover.csv"
    )
    append_audit(conn, run_id=run_id, node_id="handover", event_type="run_finished",
                 payload={"items": len(rows), "write_requests": result.write_requests,
                          "draft_requests": result.draft_requests, "degraded": degraded})

    if own_conn:
        conn.close()
    return result
