"""可读报告渲染（终端文本 + 结构化 dict）。"""
from __future__ import annotations

from .engine import REASON_TEXT
from .planner import PlanResult
from .writeoff import (
    BatchStock, OrderExecution, WriteOffRecord, WriteOffRequest,
    WO_REASON_TEXT,
)


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


def render_writeoff_result(req: WriteOffRequest, result) -> str:
    """渲染单次核销提交结果（接受/拒绝、幂等重送、失败原因）。"""
    rec = result.record
    lines: list[str] = []
    lines.append("=" * 78)
    head = "接受" if result.accepted else "拒绝"
    dup = "（重送命中，未重复扣减）" if result.duplicate else ""
    lines.append(f"核销单 {rec.writeoff_id} | 方案 {rec.plan_id} | "
                 f"出库时点 {rec.outbound_at.isoformat()} => {head}{dup}")
    lines.append("=" * 78)
    lines.append(f"  内容哈希: {rec.content_hash}")
    lines.append("  出库行：")
    for ln in sorted(rec.lines, key=lambda x: x.order_id):
        lines.append(f"    订单 {ln.order_id:<10} 批次 {ln.batch_id:<10} 数量 {ln.qty}")
    if rec.plan_snapshot is not None:
        snap = rec.plan_snapshot
        lines.append(f"  接受时方案快照: {snap.plan_id}({snap.status}) "
                     f"数据集={snap.dataset_name} 时点={snap.as_of}")
    if rec.failures:
        lines.append("  失败原因（整单未产生任何扣减）：")
        for f in rec.failures:
            lines.append(f"    [{f.code}] {WO_REASON_TEXT.get(f.code, f.code)}")
            lines.append(f"        {f.detail}")
    lines.append("=" * 78)
    return "\n".join(lines)


def render_writeoff_records(records: list[WriteOffRecord]) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"核销记录（共 {len(records)} 张，按提交时间）")
    lines.append("=" * 78)
    if not records:
        lines.append("  （无）")
        return "\n".join(lines)
    for rec in sorted(records, key=lambda r: (r.submitted_at, r.writeoff_id)):
        lines.append(f"{rec.writeoff_id:<10} {rec.status:<9} 方案={rec.plan_id:<8} "
                     f"出库时点={rec.outbound_at.isoformat()} "
                     f"行数={len(rec.lines)} 合计="
                     f"{sum(ln.qty for ln in rec.lines)}")
        for ln in sorted(rec.lines, key=lambda x: x.order_id):
            lines.append(f"    订单 {ln.order_id:<10} 批次 {ln.batch_id:<10} 数量 {ln.qty}")
        if rec.failures:
            for f in rec.failures:
                lines.append(f"    拒绝: [{f.code}] {f.detail}")
    lines.append("=" * 78)
    return "\n".join(lines)


def render_order_execution(rows: list[OrderExecution]) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("订单已出库量与剩余可执行量（已接受出库为实际事实，恰好计入一次）")
    lines.append("=" * 78)
    lines.append(f"  {'订单':<10}{'方案批次':<12}{'原分配':>8}{'已出库':>8}{'剩余可执行':>12}")
    for r in rows:
        lines.append(f"  {r.order_id:<10}{(r.allocated_batch or '-'):<12}"
                     f"{r.allocated_qty:>8}{r.shipped_qty:>8}{r.remaining_qty:>12}")
    lines.append("=" * 78)
    return "\n".join(lines)


def render_stock(rows: list[BatchStock], at=None) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    title = "批次库存（账面 / 已核销出库 / 实际库存）"
    if at is not None:
        title += f" 截至 {at.isoformat()}"
    lines.append(title)
    lines.append("=" * 78)
    lines.append(f"  {'批次':<12}{'账面余量':>10}{'已出库':>10}{'实际库存':>10}")
    for r in rows:
        lines.append(f"  {r.batch_id:<12}{r.book_balance:>10}"
                     f"{r.outbound_qty:>10}{r.on_hand:>10}")
    lines.append("=" * 78)
    return "\n".join(lines)
