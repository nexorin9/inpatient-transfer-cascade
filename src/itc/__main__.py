"""CLI 入口：桌面程序启动 + 运维排障通道（doctor / run / ledger / handover / replay）。

不设对外 HTTP 服务面；CLI 只作为桌面形态之外的排障与自检通道。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from itc.config import ConfigurationError, doctor_report, get_settings
from itc.store.db import connect, init_schema


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_doctor(_args) -> int:
    cfg = get_settings()
    report = doctor_report(cfg)
    report["his_endpoint_reachable"] = "未检测（未配置 HIS 凭据）"
    if cfg.his_ready:
        try:
            import httpx

            with httpx.Client(timeout=3.0) as probe:
                resp = probe.get(cfg.his_base_url)
            report["his_endpoint_reachable"] = f"HTTP {resp.status_code}"
        except Exception as exc:
            report["his_endpoint_reachable"] = f"不可达：{type(exc).__name__}: {exc}"
    _print_json(report)
    if not cfg.his_ready:
        print("HIS 出站未配置：请设置 HIS_BASE_URL / HIS_API_TOKEN 后再跑真实转科单。")
    if not cfg.llm_ready:
        print("模型服务未配置：语义判定项将降级为『待人工判定』，不发停嘱类写请求。")
    return 0


def _cmd_run(args) -> int:
    from itc.pipeline import run_transfer

    try:
        result = run_transfer(
            args.transfer,
            confirm=bool(args.confirm),
            sample_data=bool(args.sample_data),
            out_dir=args.out,
            settings=get_settings(itc_state_db=args.db) if args.db else None,
        )
    except ConfigurationError as exc:
        print(f"配置错误：{exc}")
        print("提示：用 --sample-data 可跑脱敏样例最小链路（不出网、不发写请求）。")
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"运行失败：{type(exc).__name__}: {exc}")
        return 1
    _print_json(result.to_dict())
    print(f"产物目录：{args.out or 'outputs/'}（牵动范围清单 / 逐条处置清单 / 停嘱草稿 / 交接核对单）")
    return 0


def _open_conn(args):
    cfg = get_settings(itc_state_db=args.db) if getattr(args, "db", None) else get_settings()
    conn = connect(cfg.state_db_path)
    init_schema(conn)
    return conn


def _cmd_ledger(args) -> int:
    from itc.store.ledger import query_ledger

    conn = _open_conn(args)
    rows = query_ledger(
        conn,
        transfer_no=args.transfer or None,
        state=args.status or None,
        execute_dept_code=args.dept or None,
        from_ts=args.from_ts or None,
        to_ts=args.to_ts or None,
    )
    conn.close()
    _print_json({"count": len(rows), "rows": rows[:200]})
    return 0


def _cmd_handover(args) -> int:
    from itc.ui.handover import export_handover

    transfer = json.loads(Path(args.transfer).read_text(encoding="utf-8"))
    conn = _open_conn(args)
    path = export_handover(conn, transfer["transfer_no"], args.out)
    conn.close()
    print(f"交接核对单已导出：{path}")
    return 0


def _cmd_replay(args) -> int:
    from itc.graph.replay import replay_run

    conn = _open_conn(args)
    summary = replay_run(conn, args.run)
    conn.close()
    _print_json(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m itc", description="住院转科连带处置系统 · 桌面程序与运维排障通道"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="检查配置齐备性与 HIS 端点可达性")

    run = sub.add_parser("run", help="跑一张转科单的完整链路")
    run.add_argument("--transfer", required=True, help="转科单 JSON 路径")
    run.add_argument("--confirm", action="store_true", help="逐条确认后写回 HIS")
    run.add_argument("--sample-data", action="store_true", help="用 fixtures 脱敏样例跑最小链路（不出网）")
    run.add_argument("--out", help="产物输出目录")
    run.add_argument("--db", help="状态库路径")

    ledger = sub.add_parser("ledger", help="处置台账查询")
    ledger_sub = ledger.add_subparsers(dest="ledger_command")
    listing = ledger_sub.add_parser("list", help="列出台账条目")
    listing.add_argument("--transfer")
    listing.add_argument("--status")
    listing.add_argument("--dept")
    listing.add_argument("--from", dest="from_ts")
    listing.add_argument("--to", dest="to_ts")
    listing.add_argument("--db")

    handover = sub.add_parser("handover", help="导出转科交接核对单")
    handover.add_argument("--transfer", required=True, help="转科单 JSON 路径")
    handover.add_argument("--out", default="outputs/handover.csv")
    handover.add_argument("--db")

    replay = sub.add_parser("replay", help="按图运行 id 重放（只读）")
    replay.add_argument("--run", required=True)
    replay.add_argument("--db")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        from itc.app import main as gui_main

        return gui_main()
    handlers = {
        "doctor": _cmd_doctor,
        "run": _cmd_run,
        "ledger": _cmd_ledger,
        "handover": _cmd_handover,
        "replay": _cmd_replay,
    }
    return int(handlers[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
