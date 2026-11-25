"""命令行入口。

示例：
  python -I cli.py plan --input sample_inputs/demo.json --as-of 2026-06-01T00:00:00Z
  python -I cli.py recalc --input ... --as-of ... --plan-id P2 --registry output/registry.json
  python -I cli.py history --registry output/registry.json
  python -I cli.py ledger --input ... --as-of ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from batch_alloc import (  # noqa: E402
    load_dataset, parse_dt, Engine, solve, PlanRegistry, verify_plan,
    render_plan, InvalidHistory, InputError, to_plain,
)


def cmd_plan(args: argparse.Namespace) -> int:
    ds = load_dataset(args.input)
    at = parse_dt(args.as_of, "as_of")
    engine = Engine(ds, at, args.rule_version)
    result = solve(engine, args.plan_id, args.rule_version, ds.input_hash)
    verification = None
    if not args.no_verify:
        verification = verify_plan(ds, result, at, args.rule_version,
                                   brute_force=not args.no_bruteforce)
    print(render_plan(result, verification))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(to_plain(result), fh, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"结构化结果已写入 {args.out}")
    if args.registry:
        reg = PlanRegistry(args.registry)
        reg.publish(result, datetime.now(timezone.utc))
        print(f"方案已登记到 {args.registry}")
    return 0 if (verification is None or verification["ok"]) else 2


def cmd_recalc(args: argparse.Namespace) -> int:
    ds = load_dataset(args.input)
    at = parse_dt(args.as_of, "as_of")
    reg = PlanRegistry(args.registry)
    result = reg.recalc(ds, at, args.plan_id, args.rule_version,
                        datetime.now(timezone.utc))
    verification = verify_plan(ds, result, at, args.rule_version,
                               brute_force=not args.no_bruteforce)
    print(render_plan(result, verification))
    return 0 if verification["ok"] else 2


def cmd_history(args: argparse.Namespace) -> int:
    reg = PlanRegistry(args.registry)
    for p in reg.history():
        r = p["result"]
        print(f"{p['plan_id']:<8} {p['status']:<10} 创建={p['created_at']} "
              f"时点={r['as_of']} 规则版本={r['rule_version']} "
              f"满足={len(r['allocations'])}单/{r['objective'][-1]}件 "
              f"取代={p.get('supersedes')}")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    ds = load_dataset(args.input)
    at = parse_dt(args.as_of, "as_of")
    engine = Engine(ds, at, args.rule_version)
    print(f"业务时点 {at.isoformat()} 的事件账本（按事件身份去重）：")
    for e in engine.ledger.entries:
        flag = "入账" if e.counted else "拒绝"
        print(f"  [{flag}] {e.effective_at.isoformat()} {e.event_id:<8} "
              f"{e.etype.value:<8} 批次={e.batch_id:<8} 变动={e.delta:>4} "
              f"余额={e.balance_after}"
              + (f"  原因={e.reject_reason}" if e.reject_reason else ""))
    print("账面余量：", engine.ledger.balances)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批次可执行分配（规则版本/业务时点驱动）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="生成分配方案")
    p.add_argument("--input", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--rule-version", default=None)
    p.add_argument("--plan-id", default="P1")
    p.add_argument("--registry", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--no-bruteforce", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("recalc", help="撤回旧方案并按修订后输入重算")
    p.add_argument("--input", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--rule-version", default=None)
    p.add_argument("--plan-id", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--no-bruteforce", action="store_true")
    p.set_defaults(func=cmd_recalc)

    p = sub.add_parser("history", help="查看新旧方案（分别可查）")
    p.add_argument("--registry", required=True)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("ledger", help="只查看事件账本与账面余量")
    p.add_argument("--input", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--rule-version", default=None)
    p.set_defaults(func=cmd_ledger)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (InvalidHistory, InputError) as exc:
        print(f"输入/历史被拒绝: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
