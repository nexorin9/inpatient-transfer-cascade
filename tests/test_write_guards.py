"""边界护栏：角色越权、写接口白名单、提交/签名路径静态扫描。"""
from __future__ import annotations

from pathlib import Path

import pytest

from itc.store import guards

SRC = Path(__file__).resolve().parents[1] / "src" / "itc"


def test_role_permissions():
    assert guards.guard_write("护士长", "confirm") is True
    assert guards.guard_write("护士长", "write_back") is True
    assert guards.guard_write("护士长", "create_draft") is True
    for role in ("医师", "经办", "住院服务中心经办", "信息科", "未知角色"):
        with pytest.raises(guards.WriteGuardError):
            guards.guard_write(role, "write_back")
        with pytest.raises(guards.WriteGuardError):
            guards.guard_write(role, "confirm")
    with pytest.raises(guards.WriteGuardError):
        guards.guard_write("医师", "create_draft")  # 非停嘱角色不得写停嘱草稿


def test_draft_is_never_submittable():
    for role in ("护士长", "医师", "系统"):
        for action in ("submit", "submit_order", "submit_orders", "submit_draft",
                       "submit_stop_draft", "sign", "sign_order", "sign_draft",
                       "commit_order"):
            with pytest.raises(guards.WriteGuardError):
                guards.guard_write(role, action)


def test_writable_paths_whitelist():
    assert guards.WRITABLE_PATHS == (
        "/api/v1/orders/{order_id}/execution-site",
        "/api/v1/orders/drafts",
    )
    assert set(guards.WRITABLE_PATHS) == {
        "/api/v1/orders/{order_id}/execution-site",
        "/api/v1/orders/drafts",
    }
    assert guards.is_writable_path("/api/v1/orders/ORD-1/execution-site") is True
    assert guards.is_writable_path("/api/v1/orders/{order_id}/execution-site") is True
    assert guards.is_writable_path("/api/v1/orders/drafts") is True
    assert guards.is_writable_path("/api/v1/orders/drafts?x=1") is True
    for path in ("/api/v1/orders/ORD-1/submit", "/api/v1/orders/ORD-1/sign",
                 "/api/v1/orders/ORD-1", "/api/v1/transfers/T-1",
                 "/api/v1/inpatients/1/orders"):
        assert guards.is_writable_path(path) is False
        with pytest.raises(guards.WriteGuardError):
            guards.assert_writable_path(path)
        with pytest.raises(guards.WriteGuardError):
            guards.guard_write("护士长", path)


def test_static_scan_only_existing_files():
    # 默认目标（writer.py 与 ui/）若尚未由别组创建，按存在性判断跳过。
    assert guards.assert_no_submit_paths() is True
    missing = SRC / "his" / "__definitely_missing_writer__.py"
    assert not missing.exists()
    assert guards.assert_no_submit_paths([missing]) is True
    assert guards.assert_no_submit_paths([SRC / "ui" / "__missing__.py"]) is True


def test_inline_submit_example_is_detected():
    example = (
        "def handle():\n"
        "    client.submit_order(order_no)\n"
        "    writer.sign_order('ORD-1')\n"
        "    return 1\n"
    )
    hits = guards.find_forbidden_calls(example)
    assert {h["name"] for h in hits} == {"submit_order", "sign_order"}
    assert all(h["line"] >= 1 for h in hits)
    with pytest.raises(guards.SubmitPathViolation) as exc:
        guards.assert_no_submit_paths([example])
    assert "submit_order" in str(exc.value) and "sign_order" in str(exc.value)


def test_comments_and_strings_do_not_false_positive():
    clean = (
        "# 说明：本文件不含 submit_order( 调用\n"
        "DOC = 'sign_order( 只是文案'\n"
        "def handle():\n"
        "    return create_stop_draft(content)\n"
    )
    assert guards.find_forbidden_calls(clean) == []


def test_temp_source_file_with_submit_call_is_detected(tmp_path):
    bad = tmp_path / "bad_writer.py"
    bad.write_text("def go():\n    his.submit_order('ORD-1')\n", encoding="utf-8")
    with pytest.raises(guards.SubmitPathViolation):
        guards.assert_no_submit_paths([bad])
    good = tmp_path / "good_writer.py"
    good.write_text(
        "def go():\n    client.post('/api/v1/orders/drafts', json={'status': 'DRAFT'})\n",
        encoding="utf-8",
    )
    assert guards.assert_no_submit_paths([good]) is True


def test_real_writer_and_ui_have_no_submit_paths():
    """真实源码护栏：已存在的 writer.py 与 ui/ 文件中不得有提交/签名调用。"""
    targets = []
    writer = SRC / "his" / "writer.py"
    if writer.is_file():
        targets.append(writer)
    ui_dir = SRC / "ui"
    if ui_dir.is_dir():
        targets.extend(sorted(p for p in ui_dir.rglob("*.py") if p.is_file()))
    assert targets, "若文件尚未创建，本用例应跳过扫描而非伪造通过"
    assert guards.assert_no_submit_paths(targets) is True
