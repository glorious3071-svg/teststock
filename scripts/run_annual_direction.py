"""年初定方向 Agent — 交互式 CLI

推荐流程（一条命令走完）:
  python scripts/run_annual_direction.py run 2011

分步命令（可选）:
  prepare  — 仅查看数据就绪情况
  start    — 收集数据 + LLM 初稿（不进入对话）
  chat     — 继续已有会话
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.annual_direction.agent import (  # noqa: E402
    continue_session,
    format_allocation_summary,
    load_session,
    prepare_context,
    print_latest_reply,
    print_session_header,
    start_session,
)
from agents.annual_direction.gaps import gaps_report  # noqa: E402
from agents.annual_direction.mode import resolve_mode  # noqa: E402


def add_mode_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        choices=("auto", "backtest", "live"),
        default="auto",
        help="auto: 早于今年的年份视为回测（禁用网络搜索、强化知识截止）",
    )


def add_common_flags(parser: argparse.ArgumentParser) -> None:
    add_mode_arg(parser)
    parser.add_argument("--no-web", action="store_true", help="不联网补充缺失数据")


def run_interactive_loop(session) -> int:
    """追问循环，直到用户定稿或退出。"""
    print("--- 进入多轮对话 ---")
    print("可追问宏观逻辑、调整仓位/ETF；满意后回复「确认」或「定稿」。")
    print("（输入 quit 暂存退出，下次 run/chat 可继续）\n")

    while not session.finalized:
        try:
            user_input = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n会话已保存，下次可继续。")
            return 0
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("会话已保存，下次可继续。")
            return 0

        print("\n思考中...\n")
        session = continue_session(session, user_input)
        print_latest_reply(session)
        print()
        if session.finalized:
            print("✅ 已定稿 — 年初投资方向已确认，可用于回测。")
            return 0
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    ctx = prepare_context(
        args.year,
        enable_web=not args.no_web,
        mode=args.mode,
    )
    print(gaps_report(ctx))
    if args.json:
        import json

        print("\n--- JSON ---\n")
        print(json.dumps(ctx.to_prompt_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    session = start_session(
        args.year,
        enable_web=not args.no_web,
        use_llm=not args.no_llm,
        mode=args.mode,
    )
    print_session_header(session)
    print_latest_reply(session)
    resolved = resolve_mode(args.year, mode=args.mode)
    print(
        f"\n会话已保存。继续对话: "
        f"python scripts/run_annual_direction.py run {args.year} --mode {args.mode}"
    )
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    session = load_session(args.year, mode=args.mode)
    if not session:
        print(
            f"未找到 {args.year} 年会话，请先运行: "
            f"python scripts/run_annual_direction.py run {args.year} --mode {args.mode}"
        )
        return 1

    if args.message:
        session = continue_session(session, args.message)
        print_session_header(session)
        print_latest_reply(session)
        if session.finalized:
            print("\n✅ 已定稿")
        return 0

    print_session_header(session)
    if session.finalized:
        print_latest_reply(session)
        print("\n✅ 该年度已定稿。使用 --fresh 可重新讨论。")
        return 0

    return run_interactive_loop(session)


def cmd_run(args: argparse.Namespace) -> int:
    """收集数据 → LLM 初稿 → 多轮追问 → 定稿（推荐入口）。"""
    resolved = resolve_mode(args.year, mode=args.mode)
    existing = None if args.fresh else load_session(args.year, mode=args.mode)

    # Step 1: 收集信息
    print(f"【1/3】收集 {args.year} 年定方向数据（知识截止 {resolved.as_of_date}）...")
    if args.verbose:
        ctx = prepare_context(args.year, enable_web=not args.no_web, mode=args.mode)
        print(gaps_report(ctx))
        print()

    need_start = (
        existing is None
        or args.fresh
        or not any(m.get("role") == "assistant" for m in (existing.messages if existing else []))
        or (existing and existing.finalized and args.fresh)
    )

    if existing and existing.finalized and not args.fresh:
        print_session_header(existing)
        print_latest_reply(existing)
        print("\n✅ 该年度已定稿。加 --fresh 可重新讨论。")
        return 0

    if existing and not need_start:
        session = existing
        print("【2/3】恢复已有会话（跳过重新调用模型）")
    else:
        if args.fresh and existing:
            print("【2/3】--fresh：重新收集数据并调用模型...")
        else:
            print("【2/3】调用模型生成初稿...")
        session = start_session(
            args.year,
            enable_web=not args.no_web,
            use_llm=True,
            mode=args.mode,
        )

    print_session_header(session)
    print_latest_reply(session)

    if session.finalized:
        print("\n✅ 已定稿。")
        return 0

    if args.no_interactive:
        print(
            f"\n【3/3】跳过对话（--no-interactive）。"
            f"继续: python scripts/run_annual_direction.py run {args.year} --mode {args.mode}"
        )
        return 0

    print()
    return run_interactive_loop(session)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="年初定方向 Agent — 收集数据、模型初稿、多轮追问、达成共识定稿",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run",
        help="推荐：收集数据 → 模型初稿 → 交互追问 → 定稿",
    )
    p_run.add_argument("year", type=int)
    add_common_flags(p_run)
    p_run.add_argument("--fresh", action="store_true", help="忽略已有会话，重新生成初稿")
    p_run.add_argument("--no-interactive", action="store_true", help="仅输出初稿，不进入对话")
    p_run.add_argument("--verbose", action="store_true", help="显示数据就绪报告")
    p_run.set_defaults(func=cmd_run)

    p_prepare = sub.add_parser("prepare", help="仅拉取数据并生成缺口报告")
    p_prepare.add_argument("year", type=int)
    add_common_flags(p_prepare)
    p_prepare.add_argument("--json", action="store_true", help="输出完整数据 JSON")
    p_prepare.set_defaults(func=cmd_prepare)

    p_start = sub.add_parser("start", help="收集数据 + LLM 初稿（不进入对话）")
    p_start.add_argument("year", type=int)
    add_common_flags(p_start)
    p_start.add_argument("--no-llm", action="store_true", help="仅输出数据包，不调用 LLM")
    p_start.set_defaults(func=cmd_start)

    p_chat = sub.add_parser("chat", help="继续已有会话")
    p_chat.add_argument("year", type=int)
    add_mode_arg(p_chat)
    p_chat.add_argument("-m", "--message", help="单条消息（非交互）")
    p_chat.set_defaults(func=cmd_chat)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
