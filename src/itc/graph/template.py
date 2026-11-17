"""转科处置图模板：加载、版本迁移（幂等）与模板来源记录。

对应 ComfyUI app/subgraph_manager.py:92 get_blueprint_subgraphs() 读 blueprints/*.json 模板：
本轮把「转科单处置图」做成可存盘的模板 JSON，并支持旧版本向当前 schema 迁移。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

# 当前模板版本；迁移函数把所有可识别旧版本统一升级到此版本
TEMPLATE_VERSION = "2"

# 允许迁移的历史版本（未知版本一律拒绝并给出可读原因）
LEGACY_VERSIONS = ("1", "1.0", "v1")

DEFAULT_TEMPLATE_ID = "inpatient-transfer-cascade/default"


class TemplateVersionError(ValueError):
    """模板版本无法识别或无法迁移。"""


# 旧模板里可能出现的类名 -> 当前类名
_LEGACY_TYPE_MAP = {
    "FetchOrders": "fetch_orders",
    "FetchSpecimens": "fetch_specimens",
    "FetchCharges": "fetch_charges",
    "RuleJudge": "rule_judge",
    "ModelJudge": "model_judge",
    "WriteExecutionSite": "change_site",
    "StopDraft": "stop_draft",
    "ExternalBlock": "external_block",
    "SpecimenRoute": "specimen_route",
    "BedFeeHint": "bed_fee_hint",
}


def _default_nodes() -> dict:
    """四类节点：取数 / 判定 / 写回 / 阻断。"""
    return {
        "fetch_orders": {"class_type": "fetch_orders", "source": "fetch", "inputs": {"inpatient_no": ""}},
        "fetch_specimens": {"class_type": "fetch_specimens", "source": "fetch", "inputs": {"inpatient_no": ""}},
        "fetch_charges": {"class_type": "fetch_charges", "source": "fetch", "inputs": {"inpatient_no": ""}},
        "rule_judge": {"class_type": "rule_judge", "source": "judge", "inputs": {}},
        "model_judge": {"class_type": "model_judge", "source": "judge", "inputs": {}},
        "change_site": {"class_type": "change_site", "source": "writeback", "inputs": {}},
        "stop_draft": {"class_type": "stop_draft", "source": "writeback", "inputs": {}},
        "specimen_route": {"class_type": "specimen_route", "source": "judge", "inputs": {}},
        "bed_fee_hint": {"class_type": "bed_fee_hint", "source": "judge", "inputs": {}},
        "external_block": {"class_type": "external_block", "source": "block", "inputs": {}},
    }


DEFAULT_TEMPLATE = {
    "template_id": DEFAULT_TEMPLATE_ID,
    "template_version": TEMPLATE_VERSION,
    "nodes": _default_nodes(),
    "links": [],
    "meta": {
        "description": "转科连带处置：取数 / 判定 / 写回 / 阻断",
        "migrated_from": TEMPLATE_VERSION,
    },
}


def load_template(path_or_dict):
    """从 JSON 路径或 dict 载入原始模板（保留原始版本，交由调用方校验 / 迁移）。"""
    if isinstance(path_or_dict, (str, Path)):
        return json.loads(Path(path_or_dict).read_text(encoding="utf-8"))
    if isinstance(path_or_dict, dict):
        return copy.deepcopy(path_or_dict)
    raise TemplateVersionError(f"模板来源类型不支持：{type(path_or_dict).__name__}")


def _raw_version(raw: dict) -> str:
    for key in ("template_version", "version", "schema_version"):
        if key in raw and raw[key] is not None:
            return str(raw[key])
    return TEMPLATE_VERSION


def known_version(version) -> bool:
    v = str(version)
    return v == TEMPLATE_VERSION or v in LEGACY_VERSIONS


def _normalize_nodes(raw: dict) -> dict:
    """把 v1 的 steps 列表 / 旧类名迁移成当前 nodes 字典；已是当前形态则原样规范化。"""
    nodes = raw.get("nodes")
    if isinstance(nodes, dict) and nodes:
        out = {}
        for nid, info in nodes.items():
            item = copy.deepcopy(info) if isinstance(info, dict) else {"class_type": str(info)}
            ct = str(item.get("class_type", ""))
            item["class_type"] = _LEGACY_TYPE_MAP.get(ct, ct)
            out[str(nid)] = item
        return out
    if isinstance(nodes, list) and nodes:
        out = {}
        for i, n in enumerate(nodes):
            if isinstance(n, dict):
                nid = str(n.get("id") or n.get("node_id") or f"node_{i}")
                ct = str(n.get("class_type") or n.get("type") or "rule_judge")
                out[nid] = {
                    "class_type": _LEGACY_TYPE_MAP.get(ct, ct),
                    "source": str(n.get("source", "")),
                    "inputs": dict(n.get("inputs", {})),
                }
            else:
                out[f"node_{i}"] = {"class_type": str(n), "source": "", "inputs": {}}
        return out
    steps = raw.get("steps") or []
    if steps:
        out = {}
        for i, step in enumerate(steps):
            if isinstance(step, dict):
                nid = str(step.get("id") or step.get("node_id") or f"node_{i}")
                ct = str(step.get("class_type") or step.get("type") or "rule_judge")
                out[nid] = {
                    "class_type": _LEGACY_TYPE_MAP.get(ct, ct),
                    "source": str(step.get("source", "")),
                    "inputs": dict(step.get("inputs", {})),
                }
            else:
                out[f"node_{i}"] = {"class_type": str(step), "source": "", "inputs": {}}
        return out
    return copy.deepcopy(DEFAULT_TEMPLATE["nodes"])


def _normalize_links(raw: dict) -> list:
    out = []
    for lk in raw.get("links") or []:
        if isinstance(lk, dict):
            frm = str(lk.get("from_node_id") or lk.get("from") or lk.get("source") or "")
            to = str(lk.get("to_node_id") or lk.get("to") or lk.get("target") or "")
            if frm and to:
                out.append([frm, to])
        elif isinstance(lk, (list, tuple)) and len(lk) >= 2:
            frm = str(lk[0])
            to = str(lk[2]) if len(lk) >= 3 else str(lk[1])
            out.append([frm, to])
    return out


def migrate_template(raw) -> dict:
    """把任意可识别版本的模板迁移到当前 schema；幂等（连跑两次结果一致）。

    未知版本抛 TemplateVersionError，错误信息包含版本号与当前支持范围（可读原因）。
    """
    if not isinstance(raw, dict):
        raw = load_template(raw)
    version = _raw_version(raw)
    if not known_version(version):
        raise TemplateVersionError(
            f"未知模板版本 {version!r}，无法迁移；当前支持 {TEMPLATE_VERSION} "
            f"及可迁移历史版本 {', '.join(LEGACY_VERSIONS)}"
        )
    meta = dict(raw.get("meta") or {})
    # 幂等关键：migrated_from 取自已有 meta，二次迁移不会变化
    meta.setdefault("migrated_from", version)
    meta.setdefault("description", DEFAULT_TEMPLATE["meta"]["description"])
    return {
        "template_id": str(raw.get("template_id") or DEFAULT_TEMPLATE_ID),
        "template_version": TEMPLATE_VERSION,
        "nodes": _normalize_nodes(raw),
        "links": _normalize_links(raw),
        "meta": meta,
    }


def template_source(raw) -> tuple[str, str]:
    """返回 (模板 id, 模板版本)，供每次入图记录来源、审计可查。"""
    if not isinstance(raw, dict):
        raw = load_template(raw)
    return (
        str(raw.get("template_id") or DEFAULT_TEMPLATE_ID),
        _raw_version(raw),
    )
