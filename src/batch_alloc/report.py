"""可读报告渲染（终端文本 + 结构化 dict）。"""
from __future__ import annotations

from .engine import REASON_TEXT
from .planner import PlanResult
from .writeoff import (
    BatchStock, OrderExecution, WriteOffRecord, WriteOffRequest,
    WO_REASON_TEXT,
)


def render_recall(report: dict) -> str:
    """召回清单终端文本（与 JSON 同口径；数量可能达十亿件，只展示汇总）。"""
    def fmt(m: dict) -> str:
        return "、".join(f"{k}={v}" for k, v in m.items()) or "-"

    lines = ["=" * 96]
    lines.append(f"召回清单（只读） | 数据集 {report['dataset']} | 业务时点 "
                 f"{report['as_of'].isoformat()}")
    lines.append(f"召回来源（原料入库事件）: {', '.join(report['recall_sources'])}")
    lines.append("=" * 96)

    lines.append("【已出库订单：尚未收回的受影响货物】")
    if report["affected_orders"]:
        lines.append(f"  {'核销单':<14}{'订单':<10}{'批次':<10}{'出库量':>12}"
                     f"{'未收回':>12}  各来源/未收回来源")
        for r in report["affected_orders"]:
            lines.append(f"  {r['writeoff_id']:<14}{r['order_id']:<10}{r['batch_id']:<10}"
                         f"{r['qty']:>12}{r['unrecovered_qty']:>12}  "
                         f"{fmt(r['by_source'])} | {fmt(r['unrecovered_by_source'])}")
    else:
        lines.append("  （无）")

    lines.append("")
    lines.append("【仍有受影响库存的批次】")
    if report["affected_stock"]:
        lines.append(f"  {'批次':<12}{'在库':>12}{'受影响':>12}  各来源")
        for r in report["affected_stock"]:
            lines.append(f"  {r['batch_id']:<12}{r['on_hand']:>12}"
                         f"{r['affected_qty']:>12}  {fmt(r['by_source'])}")
    else:
        lines.append("  （无）")

    if report.get("in_transit"):
        lines.append("")
        lines.append("【已领用、尚未产出（加工中）的受影响货物】")
        for r in report["in_transit"]:
            lines.append(f"  加工 {r['process_id']} 领用 {r['qty']} 件 "
                         f"（{', '.join(r['issue_events'])}）来源 {fmt(r['by_source'])}")

    lines.append("")
    lines.append("【已收回并重新入库的受影响货物】")
    if report["recovered"]:
        for r in report["recovered"]:
            lines.append(f"  退回 {r['return_event_id']} 收回 {r['qty']} 件 "
                         f"（{r['writeoff_id']}/{r['order_id']}）来源 {fmt(r['by_source'])}")
    else:
        lines.append("  （无）")

    t = report["totals"]
    lines.append("")
    lines.append("【汇总（同一来源多路汇合只算一次）】")
    lines.append(f"  当前受影响：在库={t['affected_stock']}  未收回={t['affected_unrecovered']}  "
                 f"加工中={t['affected_in_transit']}  => 总量 {t['affected_outstanding']}")
    lines.append(f"  流量（已含于在库，不重复相加）：已出库受影响={t['affected_shipped']}  "
                 f"其中已收回={t['affected_recovered']}")
    lines.append("=" * 96)
    return "\n".join(lines)


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
