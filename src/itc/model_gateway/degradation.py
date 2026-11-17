"""模型不可达降级编排（spec §5、契约 §2 M）。

模型不可达时：规则可判项照常可确认，语义判定项（含停嘱草稿）一律转「待人工
判定」，停嘱类写请求恒 0，语义项不得自动推进到 awaiting_confirm。降级标记
degraded / degraded_reason 随处置项落到台账。模型恢复后，未决语义项可重新判定，
已确认项不被回溯改动。

items 为处置项 dict（或带 to_dict 的对象）列表；semantic=True 表示该项依赖模型
语义判定，requires_model 为兼容别名。
"""
from __future__ import annotations


def _to_dict(item):
    return dict(item.to_dict()) if hasattr(item, "to_dict") else dict(item)


def is_semantic(item) -> bool:
    """语义判定项：需模型读懂医嘱文本（长嘱归属、停嘱草稿起草）。"""
    d = item if isinstance(item, dict) else _to_dict(item)
    return bool(d.get("semantic", d.get("requires_model", False)))


def is_confirmed(item) -> bool:
    """已确认项：有确认人或已回写，恢复时不得回溯改动。"""
    d = item if isinstance(item, dict) else _to_dict(item)
    return bool(d.get("confirmed_by")) or d.get("state") in ("written_back", "confirmed")


def should_write_stop_draft(item) -> bool:
    """是否允许发起停嘱草稿写请求。

    必须已推进到 awaiting_confirm、判定为 NEED_STOP_DRAFT、非人工项、非降级项。
    降级态语义项被改成 UNDETERMINED + manual + degraded，此判据恒为 False，
    因此降级态停嘱类写请求数为 0。
    """
    d = item if isinstance(item, dict) else _to_dict(item)
    return (
        d.get("judgement") == "NEED_STOP_DRAFT"
        and not d.get("manual")
        and not d.get("degraded")
        and d.get("state") == "awaiting_confirm"
    )


def apply_degradation(items, reason) -> list:
    """降级：规则可判项 / 已确认项原样保留；语义项标待人工判定与降级标记。"""
    out = []
    for raw in items:
        d = _to_dict(raw)
        if not is_semantic(d) or is_confirmed(d):
            out.append(d)  # 照常可确认，不做任何改动
            continue
        d["judgement"] = "UNDETERMINED"
        d["manual"] = True
        d["manual_reason"] = d.get("manual_reason") or str(reason)
        d["degraded"] = True
        d["degraded_reason"] = str(reason)
        # 语义项不得自动推进到 awaiting_confirm / written_back
        if d.get("state") in ("awaiting_confirm", "written_back"):
            d["state"] = "model_judged"
        # 停嘱草稿由模型起草，降级态不可自动产出草稿，清空避免被写成写请求
        if d.get("action") == "NEED_STOP_DRAFT" or d.get("kind") == "stop_draft":
            d["stop_draft_content"] = ""
        out.append(d)
    return out


def recover_degradation(items):
    """模型恢复：未决语义项清降级标记并标记可重新判定；已确认项不回溯。"""
    out = []
    for raw in items:
        d = _to_dict(raw)
        if is_confirmed(d):
            out.append(d)  # 已确认项逐字段不改
            continue
        if d.get("degraded"):
            d["degraded"] = False
            d["degraded_reason"] = ""
            d["rejudge"] = True  # 交由主流程重新送模型判定
        out.append(d)
    return out
