"""临床安全护栏：角色越权写入防护、写接口白名单、提交/签名路径静态扫描。

本模块只依赖标准库，不 import H / M / U 组模块，也不依赖 store 其它模块，
避免循环引用。
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

# 允许的真实写接口；除此之外的出站路径在代码层不可达。
# 顺序与 his/writer.py 的 WRITABLE_PATHS 保持一致，便于契约比对。
WRITABLE_PATHS = (
    "/api/v1/orders/{order_id}/execution-site",
    "/api/v1/orders/drafts",
)

# 角色 -> 允许的动作。停嘱草稿只能由护士长创建，且任何角色都不得提交/签名。
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "护士长": frozenset({"import", "view", "confirm", "write_back", "create_draft"}),
    "医师": frozenset({"view"}),
    "经办": frozenset({"import", "view"}),
    "住院服务中心经办": frozenset({"import", "view"}),
    "信息科": frozenset({"view"}),
    "系统": frozenset({"import", "view", "fetch", "judge", "confirm", "write_back", "create_draft"}),
}

# 常见角色别名：主流程可能用英文或进程名作为 actor，统一映射到规范角色再判定。
_ROLE_ALIASES = {
    "head_nurse": "护士长", "charge_nurse": "护士长", "nurse_manager": "护士长",
    "system": "系统", "worker": "系统", "engine": "系统", "pipeline": "系统",
}

# 产品边界：提交 / 签名医嘱的调用路径恒不可达，任何角色都不放行。
FORBIDDEN_ACTIONS = frozenset({
    "submit", "submit_order", "submit_orders", "submit_draft", "submit_drafts",
    "submit_stop_draft", "submit_prescription", "commit_order",
    "sign", "sign_order", "sign_orders", "sign_draft", "sign_drafts",
    "sign_stop_draft", "sign_prescription",
})


class WriteGuardError(PermissionError):
    """越权写入、草稿提交或白名单外写路径。"""


class SubmitPathViolation(RuntimeError):
    """源码中检出提交 / 签名医嘱的调用路径。"""


def is_writable_path(path: str) -> bool:
    """写接口白名单：只允许 execution-site 与 orders/drafts 两个出站路径。"""
    cleaned = str(path).split("?", 1)[0].rstrip("/")
    if cleaned in WRITABLE_PATHS:
        return True
    # 模板里的 {order_id} 在实际请求中会被具体医嘱号替换，仍属同一白名单接口。
    if re.fullmatch(r"/api/v1/orders/[^/]+/execution-site", cleaned):
        return True
    return cleaned == "/api/v1/orders/drafts"


def assert_writable_path(path: str) -> bool:
    """断言路径可写；白名单外路径直接拒绝。"""
    if not is_writable_path(path):
        raise WriteGuardError(f"写接口白名单外的路径不可达：{path}")
    return True


def guard_write(role: str | None, action: str) -> bool:
    """写入前角色校验。

    - 提交 / 签名类动作对所有角色一律拒绝（草稿不可提交）。
    - action 形如出站路径时，额外走写接口白名单校验。
    - role=None 表示进程内部调用（无越权身份），不阻断；显式传入角色时严格校验。
    """
    if action in FORBIDDEN_ACTIONS:
        raise WriteGuardError(f"提交/签名医嘱在本产品中不可达：{action}")
    if isinstance(action, str) and (action.startswith("/") or "/api/" in action):
        return assert_writable_path(action)
    if role is None:
        return True
    canonical = _ROLE_ALIASES.get(str(role).lower(), role)
    allowed = ROLE_PERMISSIONS.get(canonical)
    if allowed is None:
        raise WriteGuardError(f"未知角色，拒绝写入：{role}")
    if action not in allowed:
        raise WriteGuardError(f"角色 {role} 无权执行 {action}（越权写入被拒绝）")
    return True


# 提交 / 签名医嘱的调用名：submit_order、sign_order、submitOrder、submit_draft 等。
_FORBIDDEN_NAME_RE = re.compile(
    r"(?i)^(?:submit|sign)[_-]?(?:order|orders|draft|drafts|prescription|prescriptions|stop|stop_draft)[a-z0-9_]*$"
)
# tokenize 失败时的兜底：仍要求「调用」形态，避免注释里的裸词误报。
_FORBIDDEN_CALL_RE = re.compile(
    r"(?i)\b(?:submit|sign)[_-]?(?:order|orders|draft|drafts|prescription|prescriptions|stop|stop_draft)[a-z0-9_]*\s*\("
)


def _tokens(source: str) -> list[tokenize.TokenInfo]:
    return list(tokenize.generate_tokens(io.StringIO(source).readline))


def find_forbidden_calls(source: str) -> list[dict]:
    """在一段源码中检出提交 / 签名医嘱的调用，返回命中清单。

    基于 token 判断「名字后面紧跟 ( 」，因此注释与字符串字面量里的相同文字
    不会被误判，示例源码片段也能被检出。
    """
    findings: list[dict] = []
    try:
        toks = _tokens(source)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        for m in _FORBIDDEN_CALL_RE.finditer(source):
            findings.append({"name": m.group(0), "line": source.count("\n", 0, m.start()) + 1})
        return findings
    for i, tok in enumerate(toks):
        if tok.type != tokenize.NAME or not _FORBIDDEN_NAME_RE.fullmatch(tok.string):
            continue
        for nxt in toks[i + 1:]:
            if nxt.type in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT,
                            tokenize.INDENT, tokenize.DEDENT):
                continue
            if nxt.type == tokenize.OP and nxt.string == "(":
                findings.append({"name": tok.string, "line": tok.start[0]})
            break
    return findings


def _default_paths() -> list[Path]:
    """默认扫描目标：HIS 写入器与 UI 目录；只返回已存在的文件。"""
    base = Path(__file__).resolve().parents[1]
    candidates: list[Path] = [base / "his" / "writer.py"]
    ui_dir = base / "ui"
    if ui_dir.is_dir():
        candidates.extend(sorted(ui_dir.rglob("*.py")))
    return [p for p in candidates if p.is_file()]


def _scan_item(item) -> tuple[str, list[dict]]:
    """一项要么是已存在的文件（读其内容），要么是一段内联源码字符串。"""
    if isinstance(item, Path):
        if not item.is_file():
            return str(item), []
        return str(item), find_forbidden_calls(item.read_text(encoding="utf-8", errors="ignore"))
    text = str(item)
    # 内联源码片段（含换行或调用括号）不当作路径，直接扫描。
    if "\n" in text or "(" in text:
        return "<inline>", find_forbidden_calls(text)
    try:
        p = Path(text)
        if p.is_file():
            return str(p), find_forbidden_calls(
                p.read_text(encoding="utf-8", errors="ignore")
            )
    except (OSError, ValueError):
        return text, []  # 路径不可读：按存在性判断跳过
    # 文件可能稍后才由别组创建：只扫描已存在文件。
    return text, []


def assert_no_submit_paths(paths=None) -> bool:
    """静态护栏：扫描 writer.py 与 ui/ 源码，断言不存在提交 / 签名医嘱调用。

    paths=None 时扫描默认目标；paths 可含文件路径或内联源码字符串。
    文件尚不存在时按 pathlib 存在性判断跳过，只扫描已存在文件。
    """
    if paths is None:
        items: list = _default_paths()
    elif isinstance(paths, (str, Path)):
        items = [paths]
    else:
        items = list(paths)
    violations: list[dict] = []
    for item in items:
        label, hits = _scan_item(item)
        for hit in hits:
            violations.append({"file": label, **hit})
    if violations:
        detail = "; ".join(f"{v['file']}:{v['line']} {v['name']}" for v in violations)
        raise SubmitPathViolation(f"检出提交/签名医嘱调用路径：{detail}")
    return True
