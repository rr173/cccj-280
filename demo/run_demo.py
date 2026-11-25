"""端到端演示：运行 `python -I demo/run_demo.py`。

覆盖：
1. 宽用途订单占错批次 -> 窄用途订单无法满足（最优解 vs 错误的逐单先到先占）
2. 召回 / 隔离 / 解除隔离 / 质量证据缺失 / 过期 阻断
3. 退回入账条件与被拒退回
4. 保质期边界（失效日当天即过期；规则时区 Asia/Shanghai vs UTC；规则改版 v1->v2）
5. 无效数量与负库存历史必须拒绝
6. 修订后重算：撤回旧分配、量不重复、新旧方案分别可查（含召回重算）
7. 优先级字典序目标 + 独立穷举核验
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from batch_alloc import (  # noqa: E402
    load_dataset, parse_dt, Engine, solve, verify_plan, PlanRegistry,
    render_plan, to_plain, InvalidHistory, InputError, parse_dataset,
)
from batch_alloc.planner import brute_force_verify  # noqa: E402

IN = os.path.join(ROOT, "sample_inputs")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)


def section(title: str) -> None:
    print("\n" + "#" * 78)
    print(f"# {title}")
    print("#" * 78)


def fcfs_greedy(ds, at, rule_version=None):
    """反面教材：逐单先到先占（按输入顺序），不做全局优化。仅用于对照。"""
    engine = Engine(ds, at)
    # 注意：按数据集里的批次声明顺序挑第一个可行批次，才会复现“宽批次被先占”
    order_batches = list(ds.batches)
    remaining = dict(engine.ledger.balances)
    rows = []
    for o in ds.orders:  # 故意按输入顺序，不按优先级
        chosen = None
        for bid in order_batches:
            e = engine.evaluate(ds.batches[bid], o)
            if e.ok and remaining[bid] >= o.qty:
                chosen = bid
                break
        if chosen is None:
            rows.append((o.order_id, None))
        else:
            remaining[chosen] -= o.qty
            rows.append((o.order_id, chosen))
    return rows


def save_json(name, obj):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(to_plain(obj), fh, ensure_ascii=False, indent=2, sort_keys=True)
    return path


# --------------------------------------------------------------------------- #
# 1. 宽/窄用途：占错批次
# --------------------------------------------------------------------------- #
def demo_wide_narrow():
    section("场景1 宽用途订单占错批次 -> 窄用途订单无法满足")
    ds = load_dataset(os.path.join(IN, "demo_wide_narrow.json"))
    at = parse_dt("2026-06-01T00:00:00Z", "at")

    print("批次用途：B-WIDE=[A,B]（宽），B-A=[A]（窄，仅 A 单可用）")
    print("订单：O-A(用途A,100)、O-B(用途B,100)，同优先级")
    greedy = fcfs_greedy(ds, at)
    print("\n[错误做法] 逐单先到先占（O-A 先挑中 B-WIDE）：")
    for oid, bid in greedy:
        print(f"  {oid} -> {bid or '未满足'}")
    g_sat = sum(1 for _, b in greedy if b)
    print(f"  结果：仅满足 {g_sat} 单；B-WIDE 被宽用途 A 单占用，O-B 无批可用")

    engine = Engine(ds, at)
    result = solve(engine, "WN-OPT", input_hash=ds.input_hash)
    v = verify_plan(ds, result, at, brute_force=True)
    print("\n[正确做法] 按规则整体优化（分支定界 + 独立穷举核验）：")
    print(render_plan(result, v))
    save_json("plan_wide_narrow.json", result)
    assert {a.order_id: a.batch_id for a in result.allocations} == \
           {"O-A": "B-A", "O-B": "B-WIDE"}
    assert v["ok"] and v["brute_force_objective"] == tuple(result.objective)


# --------------------------------------------------------------------------- #
# 2. 质量门：召回 / 隔离 / 解除隔离 / 证据缺失 / 过期
# --------------------------------------------------------------------------- #
def demo_quality():
    section("场景2 质量门：召回、隔离（解除隔离不加账面量）、证据缺失、过期")
    ds = load_dataset(os.path.join(IN, "demo_quality.json"))
    at = parse_dt("2026-06-01T00:00:00Z", "at")
    engine = Engine(ds, at)
    result = solve(engine, "QG-1", input_hash=ds.input_hash)
    v = verify_plan(ds, result, at, brute_force=True)
    print(render_plan(result, v))
    save_json("plan_quality.json", result)

    blocked = {b.order_id: b.reason for b in result.blocked}
    blocked_detail = {b.order_id: b.detail for b in result.blocked}
    assert blocked["O-REC"] == "NO_ELIGIBLE_BATCH"
    assert blocked["O-Q"] == "NO_ELIGIBLE_BATCH"     # 仍在隔离
    assert blocked["O-NE"] == "NO_ELIGIBLE_BATCH"    # 放行无证据
    assert blocked["O-EXP"] == "NO_ELIGIBLE_BATCH"   # 已过期
    assert len(result.allocations) == 3              # O-OK1/O-OK2 同批多单 + O-Q2 解除隔离
    assert {a.order_id for a in result.allocations} == {"O-OK1", "O-OK2", "O-Q2"}
    print("阻断明细：")
    for oid, detail in blocked_detail.items():
        print(f"  {oid}: {detail}")
    # 解除隔离不增加账面量：B-Q2 在 5/1 解除隔离，账面仍只有入库的 100
    bal_q2 = engine.balance("B-Q2")
    print(f"核验 B-Q2 解除隔离后账面量（应仍为 100，状态事件不改量）：{bal_q2}")
    assert bal_q2 == 100
    assert v["ok"]


# --------------------------------------------------------------------------- #
# 3. 退回
# --------------------------------------------------------------------------- #
def demo_returns():
    section("场景3 退回：仅未拆封且验收合格才入账，其余逐笔拒绝")
    ds = load_dataset(os.path.join(IN, "demo_returns.json"))
    at = parse_dt("2026-06-01T00:00:00Z", "at")
    engine = Engine(ds, at)
    print("事件账本：")
    for e in engine.ledger.entries:
        print(f"  {'入账' if e.counted else '拒绝':<4} {e.event_id} {e.etype.value:<7} "
              f"批次={e.batch_id} 变动={e.delta:>4} 余额={e.balance_after}"
              + (f"  {e.reject_reason}" if e.reject_reason else ""))
    print("账面余量：", engine.ledger.balances)
    assert engine.balance("B-R1") == 80   # 100-30+10（另两笔退回被拒）
    assert engine.balance("B-R2") == 50

    result = solve(engine, "RT-1", input_hash=ds.input_hash)
    v = verify_plan(ds, result, at, brute_force=True)
    print(render_plan(result, v))
    save_json("plan_returns.json", result)
    sat = {a.order_id for a in result.allocations}
    assert sat == {"O-EXACT"}               # 80 恰好全额；51>50 且不可拆批
    assert {b.order_id for b in result.blocked} == {"O-SHORT"}
    assert len(result.rejected_returns) == 2
    assert v["ok"]


# --------------------------------------------------------------------------- #
# 4. 保质期边界、时区、规则改版
# --------------------------------------------------------------------------- #
def demo_shelf():
    section("场景4 保质期边界：失效日当天即过期；规则时区明确；改版 v1->v2")
    ds = load_dataset(os.path.join(IN, "demo_shelf.json"))
    # B-SH(F): 生产 2026-03-01，v1 30天，Asia/Shanghai => 失效日 2026-03-31T00:00+08:00
    #          == 2026-03-30T16:00:00Z
    # B-SH2(G): 30天，UTC => 失效日 2026-03-31T00:00:00Z
    boundary_f = parse_dt("2026-03-30T16:00:00Z", "boundary_f")
    just_before = parse_dt("2026-03-30T15:59:59Z", "just_before")
    boundary_g = parse_dt("2026-03-31T00:00:00Z", "boundary_g")

    eng = Engine(ds, just_before, rule_version="v1")
    r_before = solve(eng, "SH-BEFORE", "v1", ds.input_hash)
    print(f"[前一秒 {just_before.isoformat()}] v1 满足订单：",
          [a.order_id for a in r_before.allocations])
    assert len(r_before.allocations) == 2

    eng = Engine(ds, boundary_f, rule_version="v1")
    r_at_f = solve(eng, "SH-AT-F", "v1", ds.input_hash)
    print(f"[F 失效日当天 {boundary_f.isoformat()}] v1 满足订单：",
          [a.order_id for a in r_at_f.allocations],
          "阻断：", [(b.order_id, b.reason) for b in r_at_f.blocked])
    assert {a.order_id for a in r_at_f.allocations} == {"O-G"}
    assert r_at_f.blocked[0].reason == "NO_ELIGIBLE_BATCH"

    eng = Engine(ds, boundary_g, rule_version="v1")
    r_at_g = solve(eng, "SH-AT-G", "v1", ds.input_hash)
    print(f"[G 失效日当天 {boundary_g.isoformat()}] v1 满足订单：",
          [a.order_id for a in r_at_g.allocations])
    assert r_at_g.allocations == []

    # 规则改版 v2：F 保质期 30 -> 15 天 => 失效日 2026-03-16T00:00+08:00
    at = parse_dt("2026-03-20T00:00:00Z", "at")
    eng_v1 = Engine(ds, at, rule_version="v1")
    eng_v2 = Engine(ds, at, rule_version="v2")
    r_v1 = solve(eng_v1, "SH-V1", "v1", ds.input_hash)
    r_v2 = solve(eng_v2, "SH-V2", "v2", ds.input_hash)
    exp_v1 = eng_v1.evaluate(ds.batches["B-SH"], ds.orders[0]).expiry
    exp_v2 = eng_v2.evaluate(ds.batches["B-SH"], ds.orders[0]).expiry
    print(f"同一点 {at.isoformat()}：v1 失效日={exp_v1.isoformat()}（F 可分配="
          f"{bool(eng_v1.evaluate(ds.batches['B-SH'], ds.orders[0]).ok)}）")
    print(f"                      v2 失效日={exp_v2.isoformat()}（F 可分配="
          f"{bool(eng_v2.evaluate(ds.batches['B-SH'], ds.orders[0]).ok)}）")
    assert eng_v1.evaluate(ds.batches["B-SH"], ds.orders[0]).ok
    assert not eng_v2.evaluate(ds.batches["B-SH"], ds.orders[0]).ok
    assert {a.order_id for a in r_v2.allocations} == {"O-G"}
    save_json("plan_shelf_v1.json", r_v1)
    save_json("plan_shelf_v2.json", r_v2)


# --------------------------------------------------------------------------- #
# 5. 无效历史必须拒绝
# --------------------------------------------------------------------------- #
def demo_invalid():
    section("场景5 无效数量/负库存历史：拒绝，不用负库存补方案")
    for fname, label in [("demo_invalid_qty.json", "负数量入库"),
                         ("demo_negative_stock.json", "领用超过账面")]:
        try:
            ds = load_dataset(os.path.join(IN, fname))
            Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))
        except (InvalidHistory, InputError) as exc:
            print(f"[{label}] 已拒绝 -> {type(exc).__name__}: {exc}")
        else:
            raise AssertionError(f"{label} 应当被拒绝")


# --------------------------------------------------------------------------- #
# 6. 修订后重算：撤回旧方案、量不重复、新旧可查（规则改版 + 召回）
# --------------------------------------------------------------------------- #
def demo_recalc():
    section("场景6 修订后重算：旧分配撤回、量不重复、新旧方案分别可查")
    reg_path = os.path.join(OUT, "registry.json")
    if os.path.exists(reg_path):
        os.remove(reg_path)
    reg = PlanRegistry(reg_path)
    now = datetime.now(timezone.utc)
    at = parse_dt("2026-03-20T00:00:00Z", "at")
    ds = load_dataset(os.path.join(IN, "demo_shelf.json"))

    p1 = reg.recalc(ds, at, "RC-V1", "v1", now)
    print("首次发布 RC-V1：", [(a.order_id, a.batch_id) for a in p1.allocations])
    assert {a.order_id for a in p1.allocations} == {"O-F", "O-G"}

    # 规则修订 v1 -> v2 重算
    p2 = reg.recalc(ds, at, "RC-V2", "v2", now)
    print("规则改版重算 RC-V2：", [(a.order_id, a.batch_id) for a in p2.allocations],
          "阻断：", [b.order_id for b in p2.blocked])
    assert {a.order_id for a in p2.allocations} == {"O-G"}

    # 质量修订：B-SH2 被召回后再次重算
    with open(os.path.join(IN, "demo_shelf.json"), encoding="utf-8") as fh:
        raw = json.load(fh)
    raw["status_events"].append({
        "event_id": "EV-S-REC", "batch_id": "B-SH2", "status": "RECALLED",
        "effective_at": "2026-03-19T00:00:00Z"})
    ds3 = parse_dataset(raw)
    p3 = reg.recalc(ds3, at, "RC-V3", "v2", now)
    print("召回后重算 RC-V3：分配=", [(a.order_id, a.batch_id) for a in p3.allocations],
          "阻断=", [(b.order_id, b.reason) for b in p3.blocked])
    assert p3.allocations == []

    print("\n登记册全量历史（新旧方案分别可查）：")
    for rec in reg.history():
        r = rec["result"]
        print(f"  {rec['plan_id']:<8} {rec['status']:<10} 取代={rec.get('supersedes')} "
              f"规则版本={r['rule_version']} 目标={r['objective']}")
    active = reg.active_plans()
    assert len(active) == 1 and active[0]["plan_id"] == "RC-V3"
    # 量不重复：唯一 ACTIVE 方案的批次占用合计不超过账本余量
    v = verify_plan(ds3, p3, at, "v2", brute_force=True)
    assert v["ok"]
    print("\n独立核验 RC-V3：", v)
    save_json("plan_recalc_latest.json", p3)


# --------------------------------------------------------------------------- #
# 7. 优先级权衡
# --------------------------------------------------------------------------- #
def demo_priority():
    section("场景7 优先级字典序最大化（高优先级优先，不靠先到先占）")
    ds = load_dataset(os.path.join(IN, "demo_priority.json"))
    at = parse_dt("2026-06-01T00:00:00Z", "at")
    engine = Engine(ds, at)
    result = solve(engine, "PR-1", input_hash=ds.input_hash)
    v = verify_plan(ds, result, at, brute_force=True)
    print(render_plan(result, v))
    assert {a.order_id for a in result.allocations} == {"O-HI"}
    # 目标向量：优先级层 [100,1] 的满足数 + 总量 => (1,0,100)
    assert tuple(result.objective) == (1, 0, 100)
    assert v["ok"] and v["brute_force_objective"] == (1, 0, 100)
    save_json("plan_priority.json", result)


if __name__ == "__main__":
    demo_wide_narrow()
    demo_quality()
    demo_returns()
    demo_shelf()
    demo_invalid()
    demo_recalc()
    demo_priority()
    section("全部演示断言通过")
    print("输出目录：", OUT)
