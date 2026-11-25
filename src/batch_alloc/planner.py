"""精确分配求解器。

规则：
- 每单必须由**单个**可用于该用途的批次全额满足；不能拆批、不能部分满足；
- 同批可供多单，但批次累计分配 <= 业务时点账面余量；
- 目标按优先级从高到低做字典序最大化：先最大化各优先级层的已满足订单数，
  再最大化满足总量；同值并列任意（实现给出确定性的并列选择）；
- 禁止逐单先到先占（FCFS 一般不是最优，见演示）。

求解：带上下界剪枝的分支定界（DFS），结果可由 brute_force_verify 在小规模上独立穷举核验。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import product

from .engine import Engine, REASON_DISPLACED, REASON_NO_BATCH, REASON_TRADEOFF, REASON_TEXT, Eligibility
from .model import Order


@dataclass(frozen=True)
class Allocation:
    order_id: str
    batch_id: str
    qty: int
    rule_id: str
    expiry: datetime


@dataclass
class BlockedOrder:
    order_id: str
    reason: str
    detail: str
    per_batch: list[Eligibility] = field(default_factory=list)


@dataclass
class PlanResult:
    plan_id: str
    dataset_name: str
    rule_version: str | None
    as_of: datetime
    allocations: list[Allocation]
    blocked: list[BlockedOrder]
    objective: tuple               # (按优先级分层的满足数..., 满足总量)
    priority_levels: list[int]
    balances: dict[str, int]
    rejected_returns: list
    input_hash: str
    solver: str

    @property
    def satisfied_qty(self) -> int:
        return sum(a.qty for a in self.allocations)

    @property
    def satisfied_count(self) -> int:
        return len(self.allocations)


def _objective(orders: list[Order], levels: list[int],
               assigned: dict[str, str]) -> tuple:
    counts = {p: 0 for p in levels}
    qty = 0
    for o in orders:
        if o.order_id in assigned:
            counts[o.priority] += 1
            qty += o.qty
    return tuple(counts[p] for p in levels) + (qty,)


def solve(engine: Engine, plan_id: str, rule_version: str | None = None,
          input_hash: str = "") -> PlanResult:
    ds = engine.ds
    at = engine.at
    orders = sorted(ds.orders, key=lambda o: (-o.priority, o.order_id))
    levels = sorted({o.priority for o in orders}, reverse=True)

    # 每单的可行批次（None 表示本单不满足）；同时保留逐批诊断
    options: dict[str, list[str | None]] = {}
    diag: dict[str, list[Eligibility]] = {}
    for o in orders:
        evals = engine.candidates(o)
        diag[o.order_id] = evals
        feas = [e.batch_id for e in evals if e.ok]
        options[o.order_id] = feas + [None]

    remaining_bal: dict[str, int] = dict(engine.ledger.balances)
    assigned: dict[str, str] = {}
    best: dict[str, str] = {}
    best_obj: tuple | None = None

    def upper_bound(idx: int) -> tuple:
        """乐观上界：后续订单假设全部可满足（只多不少，保证剪枝安全）。"""
        counts = {p: 0 for p in levels}
        qty = 0
        for oid, bid in assigned.items():
            if bid is not None:
                order = next(o for o in orders if o.order_id == oid)
                counts[order.priority] += 1
                qty += order.qty
        for o in orders[idx:]:
            counts[o.priority] += 1
            qty += o.qty
        return tuple(counts[p] for p in levels) + (qty,)

    def dfs(idx: int) -> None:
        nonlocal best_obj, best
        if best_obj is not None and upper_bound(idx) <= best_obj:
            return  # 乐观上界都不优于现最佳，剪枝
        if idx == len(orders):
            obj = _objective(orders, levels, assigned)
            if best_obj is None or obj > best_obj:
                best_obj = obj
                best = dict(assigned)
            return

        order = orders[idx]
        # 先尝试各可行批次（按批次号排序 => 并列时确定），再尝试不满足
        for bid in [b for b in options[order.order_id] if b is not None] + [None]:
            if bid is None:
                dfs(idx + 1)  # 本单不满足
                continue
            if remaining_bal[bid] < order.qty:
                continue  # 超余量，该分支不可行
            remaining_bal[bid] -= order.qty
            assigned[order.order_id] = bid
            dfs(idx + 1)
            remaining_bal[bid] += order.qty
            del assigned[order.order_id]

    dfs(0)
    if best_obj is None:
        best_obj = tuple([0] * len(levels)) + (0,)

    # 组装结果
    allocations: list[Allocation] = []
    blocked: list[BlockedOrder] = []
    for o in orders:
        bid = best.get(o.order_id)
        if bid is None:
            feas = [e for e in diag[o.order_id] if e.ok]
            if not feas:
                reasons = sorted({e.reason for e in diag[o.order_id] if e.reason})
                detail = "；".join(REASON_TEXT.get(r, r) for r in reasons)
                blocked.append(BlockedOrder(o.order_id, REASON_NO_BATCH, detail,
                                            diag[o.order_id]))
            else:
                higher_satisfied = any(
                    so.priority > o.priority for so in orders
                    if so.order_id in best)
                reason = REASON_DISPLACED if higher_satisfied else REASON_TRADEOFF
                blocked.append(BlockedOrder(
                    o.order_id, reason, REASON_TEXT[reason], diag[o.order_id]))
            continue
        ev = next(e for e in diag[o.order_id] if e.batch_id == bid)
        allocations.append(Allocation(o.order_id, bid, o.qty, ev.rule_id, ev.expiry))

    return PlanResult(
        plan_id=plan_id, dataset_name=ds.name, rule_version=rule_version,
        as_of=at, allocations=allocations, blocked=blocked,
        objective=best_obj, priority_levels=levels,
        balances=dict(engine.ledger.balances),
        rejected_returns=list(engine.ledger.rejected_returns),
        input_hash=input_hash, solver="branch_and_bound",
    )


def brute_force_verify(engine: Engine) -> tuple:
    """小规模独立穷举核验，返回 (最优目标值, 最优分配列表)。

    与 branch&bound 完全独立的实现（itertools.product 全枚举），
    用于证明输出的最优值可独立核验。选项空间过大时抛错提示。
    """
    orders = sorted(engine.ds.orders, key=lambda o: (-o.priority, o.order_id))
    levels = sorted({o.priority for o in orders}, reverse=True)
    opts: list[list[str | None]] = []
    for o in orders:
        opts.append(engine.feasible_batches(o) + [None])

    space = 1
    for x in opts:
        space *= len(x)
    if space > 200_000:
        raise ValueError(f"穷举空间 {space} 超过核验上限 200000")

    best_obj = None
    best_assigns = []
    for combo in product(*opts):
        used: dict[str, int] = {}
        ok = True
        assigned: dict[str, str] = {}
        for o, bid in zip(orders, combo):
            if bid is None:
                continue
            used[bid] = used.get(bid, 0) + o.qty
            if used[bid] > engine.balance(bid):
                ok = False
                break
            assigned[o.order_id] = bid
        if not ok:
            continue
        obj = _objective(orders, levels, assigned)
        if best_obj is None or obj > best_obj:
            best_obj = obj
            best_assigns = [dict(assigned)]
        elif obj == best_obj:
            best_assigns.append(dict(assigned))
    return best_obj, best_assigns
