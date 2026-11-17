"""转科交接核对单导出：脱敏、可打印表格，条目数与台账一致。

支持 CSV 与 HTML 表格两种可打印格式（按输出路径后缀选择，默认 CSV）。
只从台账 / 注入条目里挑白名单字段：姓名、身份证、联系方式等键值一律不落文件，
住院号只留 sha256 前 12 位摘要。状态区分已回写 / 待确认 / 待人工（及阻断 / 失败）。

依赖底座 db 与 S 组 ledger 的只读查询与脱敏函数；不接触 HIS / 模型服务出站。
"""
from __future__ import annotations

import csv
import html
from pathlib import Path

from itc.store.ledger import digest_inpatient_no, query_ledger, redact_record

HANDOVER_COLUMNS = (
    "序号", "转科单号", "住院号摘要", "业务对象", "判定",
    "执行科室", "状态", "证据片段", "置信度",
)

SUPPORTED_FORMATS = ("csv", "html")

# 只用于「输出里是否意外出现敏感字段」的自检，不参与字段挑选。
PHI_TERMS = (
    "姓名", "患者姓名", "身份证", "联系方式", "联系电话", "手机号",
    "patient_name", "id_no", "id_card", "phone_no", "mobile", "contact_no",
)


def contains_phi(text: str) -> bool:
    return any(term in str(text) for term in PHI_TERMS)


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            import json
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return [value] if value else []
        return parsed if isinstance(parsed, list) else [parsed]
    return []


def handover_status(row: dict) -> str:
    """状态标注：已回写 / 待确认 / 待人工，阻断与失败单列。"""
    state = str(row.get("state") or "")
    if state == "written_back":
        return "已回写"
    if state == "failed":
        return "失败·待重写"
    if state == "blocked":
        return "阻断"
    if bool(row.get("degraded")) or str(row.get("manual_reason") or "").strip():
        return "待人工"
    if str(row.get("judgement") or "") == "UNDETERMINED":
        return "待人工"
    hits = [str(h) for h in _as_list(row.get("rule_hits"))]
    if any("no_stop" in h.lower() or "不可停" in h for h in hits):
        return "待人工"
    if str(row.get("judgement") or "") == "NEED_STOP_DRAFT":
        return "待人工"
    return "待确认"


def _business_ref(row: dict) -> str:
    kind = str(row.get("kind") or "-")
    ref = (row.get("ref") or row.get("order_no") or row.get("specimen_no")
           or row.get("charge_ref") or "-")
    return f"{kind} · {ref}"


def _confidence(row: dict) -> str:
    try:
        return f"{float(row.get('confidence') or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def first_transfer_no(conn) -> str:
    """台账里第一条记录的转科单号；供界面在未导入具体转科单时兜底。"""
    if conn is None:
        return ""
    rows = query_ledger(conn, limit=1)
    return str(rows[0].get("transfer_no") or "") if rows else ""


def handover_rows(conn, transfer_no: str, items=None) -> list[dict]:
    """构造脱敏后的核对单行；条目数与台账（或注入 items）一致。"""
    if items is not None:
        raw_rows = [x.to_dict() if hasattr(x, "to_dict") else dict(x) for x in items]
    elif conn is not None:
        raw_rows = query_ledger(conn, transfer_no=transfer_no)
    else:
        raise ValueError("导出交接核对单需要 conn 或 items 之一")

    out: list[dict] = []
    for index, raw in enumerate(raw_rows, start=1):
        # 递归脱敏：PHI 键剔除，住院号替换为摘要
        clean = redact_record(dict(raw))
        digest = str(clean.get("inpatient_no") or "")
        if not digest and raw.get("inpatient_no"):
            digest = digest_inpatient_no(raw["inpatient_no"])
        out.append({
            "序号": index,
            "转科单号": str(clean.get("transfer_no") or transfer_no or ""),
            "住院号摘要": digest,
            "业务对象": _business_ref(clean),
            "判定": str(clean.get("judgement") or "-"),
            "执行科室": str(clean.get("execute_dept_name")
                            or clean.get("execute_dept_code") or "-"),
            "状态": handover_status(clean),
            "证据片段": str(clean.get("evidence_snippet") or "-"),
            "置信度": _confidence(clean),
        })
    return out


def _format_for(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return "html" if suffix in ("html", "htm") else "csv"


def _write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HANDOVER_COLUMNS)
        for row in rows:
            writer.writerow([row[col] for col in HANDOVER_COLUMNS])


def _write_html(rows: list[dict], path: Path, transfer_no: str) -> None:
    esc = html.escape
    head = "".join(f"<th>{esc(col)}</th>" for col in HANDOVER_COLUMNS)
    body = []
    for row in rows:
        cells = "".join(f"<td>{esc(str(row[col]))}</td>" for col in HANDOVER_COLUMNS)
        body.append(f'<tr class="row">{cells}</tr>')
    document = (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        "<title>转科交接核对单</title>"
        "<style>"
        "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
        "color:#334155;margin:24px;}"
        "table{border-collapse:collapse;width:100%;font-size:14px;}"
        "caption{text-align:left;font-size:20px;font-weight:600;color:#0F172A;"
        "padding:8px 0 16px;}"
        "th{background:#F1F5F9;color:#0F172A;border:1px solid #E2E8F0;"
        "padding:8px 12px;text-align:left;}"
        "td{border:1px solid #E2E8F0;padding:8px 12px;}"
        "tr.row:nth-child(even){background:#F8FAFC;}"
        "@media print{body{margin:0;}}"
        "</style></head><body>"
        f"<table><caption>转科交接核对单 · {esc(str(transfer_no))}</caption>"
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
        "</body></html>\n"
    )
    path.write_text(document, encoding="utf-8")


def export_handover(conn, transfer_no: str, out_path, items=None) -> Path:
    """导出脱敏可打印核对单，返回写出的文件路径。

    - conn 为状态库连接时，条目取自 S 组 ledger.query_ledger（与台账一致）；
    - items 不为 None 时优先用注入条目（便于界面层直接传入当前清单）；
    - .html / .htm 输出 HTML 表格，其余后缀输出 CSV。
    """
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = handover_rows(conn, str(transfer_no), items=items)

    # 结构护栏：输出行只含白名单列，姓名 / 证件 / 联系方式等键根本不会被挑选。
    for row in rows:
        if set(row) != set(HANDOVER_COLUMNS):
            raise ValueError("核对单行含白名单外字段，已中止导出")

    if _format_for(path) == "html":
        _write_html(rows, path, str(transfer_no))
    else:
        _write_csv(rows, path)
    return path
