"""可观测性：结构化日志、错误码字典、批次运行摘要与脱敏断言（任务 49）。

本模块自包含，不 import H/M/U/S 组模块：日志落盘前在本模块内完成住院号摘要化与
PHI 字段剔除。日志写到项目 logs/ 目录（.gitignore 已排除 logs/）。

对外 API：
    ERROR_CODES                         错误码字典（6 类）
    structured_log(event) -> str        结构化 JSON 行并追加写日志
    assert_log_redacted(line)          扫描一行 / 一段日志，发现 PHI 字段即抛错
    scan_log_file(path=None) -> int    扫描整个日志文件，返回扫描行数
    write_batch_summary(conn, stats, path=None) -> Path
    log_path() -> Path
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 错误码字典（6 类，任务 49 要求齐全）
# ---------------------------------------------------------------------------

ERROR_CODES = {
    "FETCH_FAILED": "取数失败",
    "FIELD_MISSING": "字段缺失",
    "ENTITY_AMBIGUOUS": "实体歧义",
    "MODEL_UNREACHABLE": "模型不可达",
    "WRITEBACK_FAILED": "写回失败",
    "IDEMPOTENCY_HIT": "幂等命中",
}

# 批次摘要 6 项计数 -> 中文标签
BATCH_FIELDS = {
    "processed": "处理例数",
    "cache_hits": "命中缓存数",
    "blocked": "阻断数",
    "manual": "待人工数",
    "writebacks": "写回数",
    "failed": "失败数",
}

# 精确 PHI 字段名（小写）。业务字段 execute_dept_name / inpatient_digest 不含在此列。
PHI_KEYS = frozenset({
    "name", "patient_name", "real_name", "full_name",
    "id_no", "id_card", "idcard", "id_number", "identity_no", "identity",
    "ssn", "social_id",
    "phone", "phone_no", "mobile", "tel", "telephone", "contact", "contact_no",
    "address", "home_address",
})

_INPATIENT_KEYS = frozenset({"inpatient_no", "inpatientno", "inpatient_id"})


# ---------------------------------------------------------------------------
# 路径与脱敏
# ---------------------------------------------------------------------------

def _logs_dir() -> Path:
    env = os.environ.get("ITC_LOG_DIR", "").strip()
    if env:
        return Path(env)
    # src/itc/observability.py -> parents[2] = 项目根
    return Path(__file__).resolve().parents[2] / "logs"


def log_path() -> Path:
    """结构化日志文件路径（项目 logs/ 或 ITC_LOG_DIR 指定目录）。"""
    return _logs_dir() / "structured.jsonl"


def digest_inpatient_no(value) -> str:
    """住院号摘要：sha256 前 12 位，不可逆、不含原文。"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _is_phi_key(key) -> bool:
    return str(key).strip().lower() in PHI_KEYS


def _is_inpatient_key(key) -> bool:
    return str(key).strip().lower() in _INPATIENT_KEYS


def redact_record(record: dict) -> dict:
    """把一条日志事件脱敏：PHI 字段整体丢弃，住院号只留摘要。

    返回新 dict，不改原记录；嵌套 dict / list 递归处理。丢弃而非保留 [REDACTED]
    占位键，是为了让「日志不含姓名 / 身份证 / 联系方式字段」可被直接扫描断言。
    """
    out = {}
    for key, value in record.items():
        if _is_phi_key(key):
            continue  # 姓名 / 身份证 / 联系方式字段不落日志
        elif _is_inpatient_key(key):
            # 住院号不出现在日志字段里，统一折叠成 inpatient_digest
            out["inpatient_digest"] = digest_inpatient_no(value)
        elif isinstance(value, dict):
            out[key] = redact_record(value)
        elif isinstance(value, list):
            out[key] = [redact_record(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------

def structured_log(event: dict) -> str:
    """产出一行结构化 JSON 日志并追加写入 logs/，返回该行文本。

    行内固定含：ts / run_id / node_id / node_type / status / duration_ms /
    error_code / inpatient_digest。住院号只留摘要，PHI 字段被剔除或遮蔽。
    """
    if not isinstance(event, dict):
        raise ValueError("日志事件必须是 dict")
    record = {
        "ts": event.get("ts") or _now_iso(),
        "run_id": str(event.get("run_id", "")),
        "node_id": str(event.get("node_id", "")),
        "node_type": str(event.get("node_type", "")),
        "status": str(event.get("status", "")),
        "duration_ms": float(event.get("duration_ms", 0) or 0),
        "error_code": str(event.get("error_code", "") or ""),
        "inpatient_digest": str(event.get("inpatient_digest", "") or ""),
    }
    # 其余业务字段（可能含 inpatient_no / PHI）合并后统一脱敏：
    # redact_record 会把 inpatient_no 折叠成 inpatient_digest 摘要并覆盖空值
    extras = {k: v for k, v in event.items() if k not in record}
    merged = redact_record({**record, **extras})
    merged.setdefault("inpatient_digest", "")
    line = json.dumps(merged, ensure_ascii=False, sort_keys=True)
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


# ---------------------------------------------------------------------------
# 脱敏断言
# ---------------------------------------------------------------------------

def _assert_record_redacted(obj) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _is_phi_key(key):
                raise ValueError(f"日志含未脱敏的 PHI 字段：{key}")
            if _is_inpatient_key(key):
                raise ValueError(f"日志含住院号原文字段，应只留摘要：{key}")
            _assert_record_redacted(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_record_redacted(item)


def assert_log_redacted(line) -> bool:
    """扫描一行 / 一段日志，确认不含姓名 / 身份证 / 联系方式 / 住院号原文字段。

    line 可为 JSON 字符串、dict 或若干行组成的字符串。发现违规即抛 ValueError。
    """
    if isinstance(line, dict):
        _assert_record_redacted(line)
        return True
    if not isinstance(line, str):
        raise ValueError("assert_log_redacted 只接受 str / dict")
    text = line.strip()
    if not text:
        return True
    # 整段是单个 JSON 对象 / 数组
    try:
        parsed = json.loads(text)
        _assert_record_redacted(parsed)
        return True
    except json.JSONDecodeError:
        pass
    # 否则按 JSON Lines 逐行解析；非 JSON 行按占位文本跳过（结构化日志均为 JSON）
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            _assert_record_redacted(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(f"日志行不是合法 JSON，无法确认脱敏：{raw[:60]}") from exc
    return True


def scan_log_file(path=None) -> int:
    """扫描整个日志文件，逐行断言脱敏；返回扫描行数。"""
    p = Path(path) if path is not None else log_path()
    if not p.is_file():
        return 0
    count = 0
    with p.open("r", encoding="utf-8") as fh:
        for raw in fh:
            if raw.strip():
                assert_log_redacted(raw)
                count += 1
    return count


# ---------------------------------------------------------------------------
# 批次运行摘要落盘
# ---------------------------------------------------------------------------

def _default_summary_path() -> Path:
    return Path(__file__).resolve().parents[2] / "outputs" / "batch_summary.json"


def write_batch_summary(conn, stats: dict, path=None) -> Path:
    """把一批运行的摘要（6 项计数）落盘为 JSON，并尽量写入状态库。

    6 项计数：处理例数 / 命中缓存数 / 阻断数 / 待人工数 / 写回数 / 失败数。
    stats 中的其余键（耗时、规模、阈值等）原样保留。返回写入的 Path。
    """
    if not isinstance(stats, dict):
        raise ValueError("批次摘要 stats 必须是 dict")
    summary = dict(stats)
    for key in BATCH_FIELDS:
        summary.setdefault(key, 0)
    summary["field_labels"] = dict(BATCH_FIELDS)
    summary["generated_at"] = _now_iso()

    out = Path(path) if path is not None else _default_summary_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # 最佳努力：把同一份摘要写入状态库的独立表（不触碰其它组的表结构）
    if conn is not None and hasattr(conn, "execute"):
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS itc_batch_summaries ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "payload TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO itc_batch_summaries (payload, created_at) VALUES (?, ?)",
                (json.dumps(summary, ensure_ascii=False, sort_keys=True), summary["generated_at"]),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 - 文件是权威产物，DB 落库失败不影响摘要
            pass
    return out
