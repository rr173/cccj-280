"""可读报告渲染（终端文本 + 结构化 dict）。"""
from __future__ import annotations

from .engine import REASON_TEXT
from .planner import PlanResult


def render_plan(result: PlanResult, verification: dict | None = None) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"方案 {result.plan_id} | 数据集 {result.dataset_name} | "
                 f"业务时点 {result.as_of.isoformat()} | 规则版本 "
                 f"{result.rule_version or '(默认最新生效)'} | 求解器 {result.solver}")
    lines.append("=" * 78)

    lines.append("【已满足订单】每单来自单个批次，全额整数满足：")
    if result.allocations:
        lines.append(f"  {'订单':<10}{'批次':<10}{'数量':>6}  适用规则 / 失效日")
        for a in sorted(result.allocations, key=lambda x: x.order_id):
            lines.append(f"  {a.order_id:<10}{a.batch_id:<10}{a.qty:>6}  "
                         f"{a.rule_id} / {a.expiry.isoformat()}")
    else:
        lines.append("  （无）")

    lines.append("")
    lines.append("【未满足订单及阻断依据】：")
    if result.blocked:
        for b in sorted(result.blocked, key=lambda x: x.order_id):
            head = REASON_TEXT.get(b.reason, b.reason)
            lines.append(f"  {b.order_id}: [{b.reason}] {head}")
            if b.detail:
                lines.append(f"      概述: {b.detail}")
            for e in b.per_batch:
                mark = "OK " if e.ok else "X  "
                why = "" if e.ok else f" -> {REASON_TEXT.get(e.reason, e.reason)} {e.detail}"
                lines.append(f"      {mark}{e.batch_id} 账面={e.balance:<5} "
                             f"状态={e.status.value if e.status else '-'}{why}")
    else:
        lines.append("  （无）")

    lines.append("")
    lines.append("【批次账面余量与占用】：")
    usage: dict[str, int] = {}
    for a in result.allocations:
        usage[a.batch_id] = usage.get(a.batch_id, 0) + a.qty
    for bid in sorted(result.balances):
        lines.append(f"  {bid}: 账面余量={result.balances[bid]:<5} "
                     f"本方案占用={usage.get(bid, 0):<5} "
                     f"剩余可分配={result.balances[bid] - usage.get(bid, 0)}")

    if result.rejected_returns:
        lines.append("")
        lines.append("【被拒绝的退回（未入账）】：")
        for e in result.rejected_returns:
            lines.append(f"  {e.event_id} 批次 {e.batch_id}: {e.reject_reason}")

    lines.append("")
    lines.append("【最优值，可独立核验】")
    lines.append(f"  优先级层(高->低)={result.priority_levels}")
    lines.append(f"  字典序目标向量 = {list(result.objective)}")
    lines.append(f"  含义: 各优先级层满足订单数 {list(result.objective[:-1])}，"
                 f"满足总量 {result.objective[-1]}；已满足订单 {result.satisfied_count} 单")

    if verification is not None:
        lines.append("")
        lines.append("【独立核验】")
        lines.append(f"  结论: {'通过' if verification['ok'] else '失败'}")
        lines.append(f"  穷举最优向量: {verification['brute_force_objective']}")
        if verification["errors"]:
            for err in verification["errors"]:
                lines.append(f"  ERROR: {err}")
        lines.append(f"  批次占用: {verification['batch_usage']}")
    lines.append("=" * 78)
    return "\n".join(lines)
