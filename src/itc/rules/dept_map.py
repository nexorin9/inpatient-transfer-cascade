"""科室映射表加载与解析（信息科可维护的 JSON）。

规则基线（spec §5）要求：缺映射返回 None，需人工判定、不猜。
本模块还向其它规则模块提供三个共用小工具：catalog_path 归一化目录文件路径、
iter_mappings 归一化映射表、entity_to_dict 把 domain 对象或 dict 归一化为 dict。
"""
from __future__ import annotations

import json
from pathlib import Path

from itc.config import get_settings

_DEFAULT_FILENAME = "dept_map.json"
_META_KEYS = frozenset({"version", "note", "description", "_note", "_error"})


def catalog_path(path, filename):
    """把 None / 目录 / 文件路径归一化为具体的 JSON 文件路径。

    None 时取配置里的规则目录（默认包内 catalogs），各目录 JSON 共用此入口，
    信息科可通过 ITC_RULES_DIR 环境变量整体换目录维护。
    """
    if path is None:
        return Path(get_settings().rules_dir) / filename
    p = Path(path)
    return p / filename if p.is_dir() else p


def load_dept_map(path=None) -> dict:
    """读取科室映射表。path 可为文件路径或目录（目录下取 dept_map.json）。

    文件缺失、JSON 损坏或顶层不是对象时，保守返回空映射 {}：任何科室编码都
    视为缺映射，resolve_dept 返回 None，交人工判定，绝不按最近似猜。
    """
    p = catalog_path(path, _DEFAULT_FILENAME)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def iter_mappings(dept_map=None) -> dict:
    """归一化出 code -> entry 的映射字典。

    兼容三种入参：None（自动加载默认表）、带 mappings 键的完整表、纯映射字典
    （值可为名称字符串或条目字典）。元数据键与下划线开头的键被排除。
    """
    dm = dept_map if dept_map is not None else load_dept_map()
    if not isinstance(dm, dict):
        return {}
    raw = dm.get("mappings") if isinstance(dm.get("mappings"), dict) else dm
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): v
        for k, v in raw.items()
        if str(k) not in _META_KEYS and not str(k).startswith("_")
    }


def resolve_dept(code, dept_map=None):
    """把科室编码解析为规范科室名；缺映射返回 None（需人工判定，不猜）。"""
    if code is None:
        return None
    entry = iter_mappings(dept_map).get(str(code))
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        name = entry.get("canonical_name") or entry.get("name") or entry.get("canonical_code")
        return str(name) if name else None
    return None


def entity_to_dict(obj) -> dict:
    """把 domain 对象 / dict / 普通对象归一化为 dict，供各规则判定共用。

    规则判定同时接受 HisReader 产出的 domain 对象与手工构造的 dict，避免每个
    判定函数各写一套取值逻辑；缺字段时返回空串由调用方按保守口径处理。
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, dict) else {}
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {}
