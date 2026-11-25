"""命令行入口。

示例：
  python -I cli.py plan --input sample_inputs/demo.json --as-of 2026-06-01T00:00:00Z
  python -I cli.py recalc --input ... --as-of ... --plan-id P2 --registry output/registry.json
  python -I cli.py history --registry output/registry.json
  python -I cli.py ledger --input ... --as-of ...
  python -I cli.py writeoff --input sample.json --doc writeoff.json \
                            --registry output/registry.json --store output/writeoffs.json
  python -I cli.py writeoffs --store output/writeoffs.json [--status ACCEPTED]
  python -I cli.py execution --input sample.json --registry output/registry.json
  python -I cli.py stock --input sample.json --registry output/registry.json \
                          [--as-of 2026-06-01T00:00:00Z]
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
    WarehouseService, load_writeoff, render_writeoff_result,
    render_writeoff_records, render_order_execution, render_stock,
    RegistryError,
)


def cmd_plan(args: argparse.Namespace) -> int:
    ds = load_dataset(args.input)
    at = parse_dt(args.as_of, "as_of")
    if args.registry:
        # 经仓库服务加锁发布：与核销/重算共用同一串行化点，
        # 并在已存在出库事实时把实际出库计入库存后再求解。
        svc = WarehouseService(args.registry, args.store)
        aug = svc.effective_dataset(ds, at)
        engine = Engine(aug, at, args.rule_version)
        result = solve(engine, args.plan_id, args.rule_version, ds.input_hash)
        verification = None
        if not args.no_verify:
            verification = verify_plan(aug, result, at, args.rule_version,
                                       brute_force=not args.no_bruteforce)
        print(render_plan(result, verification))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(to_plain(result), fh, ensure_ascii=False, indent=2, sort_keys=True)
            print(f"结构化结果已写入 {args.out}")
        svc.registry.publish(result, datetime.now(timezone.utc))
        print(f"方案已登记到 {args.registry}（仓库服务口径，已计入核销出库事实）")
        return 0 if (verification is None or verification["ok"]) else 2

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
    return 0 if (verification is None or verification["ok"]) else 2


def cmd_recalc(args: argparse.Namespace) -> int:
    ds = load_dataset(args.input)
    at = parse_dt(args.as_of, "as_of")
    svc = WarehouseService(args.registry, args.store)
    # 重算在核销同一把锁内完成：撤回旧 ACTIVE → 基于含出库事实的账本发布新方案
    result = svc.recalc(ds, at, args.plan_id, args.rule_version,
                        datetime.now(timezone.utc))
    aug = svc.effective_dataset(ds, at)
    verification = verify_plan(aug, result, at, args.rule_version,
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


def cmd_writeoff(args: argparse.Namespace) -> int:
    ds = load_dataset(args.input)
    req = load_writeoff(args.doc)
    svc = WarehouseService(args.registry, args.store)
    result = svc.submit_writeoff(ds, req, datetime.now(timezone.utc))
    print(render_writeoff_result(req, result))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(to_plain(result.record), fh, ensure_ascii=False,
                      indent=2, sort_keys=True)
        print(f"核销记录已写入 {args.out}")
    # 0=接受（含幂等重送的已接受）；4=拒绝（含业务校验失败/幂等冲突）
    return 0 if result.accepted else 4


def cmd_writeoffs(args: argparse.Namespace) -> int:
    svc = WarehouseService(args.registry, args.store)
    print(render_writeoff_records(svc.list_writeoffs(args.status)))
    return 0


def cmd_execution(args: argparse.Namespace) -> int:
    ds = load_dataset(args.input)
    svc = WarehouseService(args.registry, args.store)
    print(render_order_execution(svc.order_execution(ds, args.plan_id)))
    return 0


def cmd_stock(args: argparse.Namespace) -> int:
    ds = load_dataset(args.input)
    svc = WarehouseService(args.registry, args.store)
    at = parse_dt(args.as_of, "as_of") if args.as_of else None
    print(render_stock(svc.stock_view(ds, at), at))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批次可执行分配与拣发核销（规则版本/业务时点驱动）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="生成分配方案")
    p.add_argument("--input", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--rule-version", default=None)
    p.add_argument("--plan-id", default="P1")
    p.add_argument("--registry", default=None)
    p.add_argument("--store", default=None, help="核销存储 JSON（默认登记册同目录 writeoffs.json）")
    p.add_argument("--out", default=None)
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--no-bruteforce", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("recalc", help="撤回旧方案并按修订后输入重算（锁内计入已核销出库）")
    p.add_argument("--input", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--rule-version", default=None)
    p.add_argument("--plan-id", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--store", default=None)
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

    p = sub.add_parser("writeoff", help="提交拣发核销单（整单接受/拒绝，幂等）")
    p.add_argument("--input", required=True, help="当前数据集 JSON（须与方案发布口径一致）")
    p.add_argument("--doc", required=True, help="核销单 JSON")
    p.add_argument("--registry", required=True)
    p.add_argument("--store", default=None)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_writeoff)

    p = sub.add_parser("writeoffs", help="查看核销记录")
    p.add_argument("--registry", required=True)
    p.add_argument("--store", default=None)
    p.add_argument("--status", choices=["ACCEPTED", "REJECTED"], default=None)
    p.set_defaults(func=cmd_writeoffs)

    p = sub.add_parser("execution", help="订单已出库量与剩余可执行量")
    p.add_argument("--input", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--store", default=None)
    p.add_argument("--plan-id", default=None)
    p.set_defaults(func=cmd_execution)

    p = sub.add_parser("stock", help="批次账面/已出库/实际库存")
    p.add_argument("--input", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--store", default=None)
    p.add_argument("--as-of", default=None, help="只计入该时点前（含）的核销出库")
    p.set_defaults(func=cmd_stock)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (InvalidHistory, InputError) as exc:
        print(f"输入/历史被拒绝: {exc}", file=sys.stderr)
        return 3
    except RegistryError as exc:
        print(f"登记册错误: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
