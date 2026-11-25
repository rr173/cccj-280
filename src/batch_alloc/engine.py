"""业务时点引擎：账面余量（事件账本）、质量状态、保质期与用途可用性。

关键不变量：
1. 数量按事件身份去重；同身份事件只处理一次。
2. 退回仅未拆封且验收合格才入账，被拒退回逐笔给出原因，绝不计入余额。
3. 放行/隔离/召回是状态事件，只改可用性、永不改账面量；解除隔离不增加账面量。
4. 任何时点余额为负 => InvalidHistory，拒绝整个历史，禁止用负库存“补”方案。
5. 失效日与规则时区由输入明确；at == 失效日 00:00 即过期。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .model import (
    Batch, Dataset, InvalidHistory, Order, QEventType, QStatus,
    QuantityEvent, ShelfRule, StatusEvent,
)

# 阻断原因码（报告中附中文说明）
REASON_REJECTED_RETURN = "REJECTED_RETURN"
REASON_NEGATIVE_HISTORY = "NEGATIVE_HISTORY"
REASON_UNKNOWN_BATCH = "UNKNOWN_BATCH"
REASON_PURPOSE = "PURPOSE_NOT_ALLOWED"
REASON_QUALITY_EVIDENCE = "QUALITY_EVIDENCE_MISSING"
REASON_RECALLED = "RECALLED"
REASON_QUARANTINED = "QUARANTINED"
REASON_EXPIRED = "EXPIRED"
REASON_INSUFFICIENT = "INSUFFICIENT_BALANCE"
REASON_NO_BATCH = "NO_ELIGIBLE_BATCH"
REASON_DISPLACED = "DISPLACED_BY_HIGHER_PRIORITY"
REASON_TRADEOFF = "OPTIMAL_TRADEOFF"

REASON_TEXT = {
    REASON_REJECTED_RETURN: "退回不满足入账条件（必须未拆封且验收合格），未计入账面",
    REASON_NEGATIVE_HISTORY: "事件历史导致负库存，历史非法，已拒绝",
    REASON_UNKNOWN_BATCH: "事件引用了不存在的批次",
    REASON_PURPOSE: "该批次用途不覆盖订单用途",
    REASON_QUALITY_EVIDENCE: "放行缺少质量证据，质量证据缺失不得分配",
    REASON_RECALLED: "批次处于召回状态，不得分配",
    REASON_QUARANTINED: "批次处于隔离状态，不得分配（解除隔离不增加账面量）",
    REASON_EXPIRED: "按用途保质期规则已过期（失效日当天即过期）",
    REASON_INSUFFICIENT: "无单个批次余量可全额满足该整数需求量（禁止拆批/部分满足）",
    REASON_NO_BATCH: "不存在任何可用于该用途的批次",
    REASON_DISPLACED: "批次须让位于更高优先级订单（整体最优下本单未满足）",
    REASON_TRADEOFF: "存在可用批次，但单批容量无法同时容纳全部订单，整体最优下本单未满足",
}


@dataclass(frozen=True)
class LedgerEntry:
    event_id: str
    batch_id: str
    etype: QEventType
    delta: int
    balance_after: int
    effective_at: datetime
    counted: bool
    reject_reason: str | None = None
    note: str = ""


@dataclass
class LedgerResult:
    balances: dict[str, int]
    entries: list[LedgerEntry]
    rejected_returns: list[LedgerEntry]


def _sorted_quantity_events(events: list[QuantityEvent]) -> list[QuantityEvent]:
    # 同点事件按身份排序，保证可重复的确定性账本
    return sorted(events, key=lambda e: (e.effective_at, e.event_id))


def build_ledger(ds: Dataset, at: datetime) -> LedgerResult:
    """构建截至时点 at 的事件账本。

    - 事件身份去重（解析阶段已保证 id 唯一，这里再按 id 防御性去重）；
    - 逐笔过账并检查负库存；
    - 退回按 sealed & accepted 决定是否入账。
    """
    balances: dict[str, int] = {b: 0 for b in ds.batches}
    entries: list[LedgerEntry] = []
    rejected: list[LedgerEntry] = []
    seen: set[str] = set()

    for ev in _sorted_quantity_events(ds.quantity_events):
        if ev.effective_at > at:
            break
        if ev.event_id in seen:  # 事件身份去重
            continue
        seen.add(ev.event_id)

        if ev.etype is QEventType.RETURN and not (bool(ev.sealed) and bool(ev.accepted)):
            why = []
            if not ev.sealed:
                why.append("已拆封")
            if not ev.accepted:
                why.append("验收不合格")
            entry = LedgerEntry(
                ev.event_id, ev.batch_id, ev.etype, 0, balances[ev.batch_id],
                ev.effective_at, counted=False,
                reject_reason=REASON_REJECTED_RETURN + ":" + "/".join(why),
                note=ev.note,
            )
            entries.append(entry)
            rejected.append(entry)
            continue

        if ev.etype in (QEventType.RECEIPT, QEventType.RETURN):
            delta = ev.qty
        else:  # ISSUE / SCRAP
            delta = -ev.qty

        new_bal = balances[ev.batch_id] + delta
        if new_bal < 0:
            raise InvalidHistory(
                f"事件 {ev.event_id}（{ev.etype.value} {ev.qty}）使批次 "
                f"{ev.batch_id} 在 {ev.effective_at.isoformat()} 出现负库存 "
                f"（当前账面 {balances[ev.batch_id]}）。无效历史须拒绝，不得用负库存补方案。"
            )
        balances[ev.batch_id] = new_bal
        entries.append(LedgerEntry(
            ev.event_id, ev.batch_id, ev.etype, delta, new_bal,
            ev.effective_at, counted=True, note=ev.note,
        ))

    return LedgerResult(balances=balances, entries=entries, rejected_returns=rejected)


def latest_status(ds: Dataset, batch_id: str, at: datetime) -> QStatus:
    """取 at 时点最新状态事件；无事件则用批次初始状态。"""
    status = ds.batches[batch_id].initial_status
    for ev in sorted(ds.status_events, key=lambda e: (e.effective_at, e.event_id)):
        if ev.batch_id == batch_id and ev.effective_at <= at:
            status = ev.status
    return status


def latest_release_evidence(ds: Dataset, batch_id: str, at: datetime) -> StatusEvent | None:
    """取 at 时点最新一条放行事件（用于核验质量证据）。"""
    rel = [e for e in ds.status_events
           if e.batch_id == batch_id and e.status is QStatus.RELEASED and e.effective_at <= at]
    if not rel:
        return None
    return sorted(rel, key=lambda e: (e.effective_at, e.event_id))[-1]


def select_rule(rules: list[ShelfRule], purpose: str, at: datetime,
                rule_version: str | None = None) -> ShelfRule:
    """选择 at 时点对 purpose 生效的规则。

    - rule_version 给定时只在该版本内挑选（规则改版可重算）；
    - 否则在所有版本中挑选生效日 <= at 的最新一条；
    - 先找用途专属规则，找不到再用 purpose is None 的兜底规则。
    """
    pool = [r for r in rules if r.version == rule_version] if rule_version else list(rules)
    active = [r for r in pool if r.effective_at <= at]
    if not active:
        raise InvalidHistory(
            f"时点 {at.isoformat()} 没有已生效的保质期规则"
            + (f"（版本 {rule_version}）" if rule_version else "")
        )

    def pick(candidates: list[ShelfRule]) -> ShelfRule | None:
        if not candidates:
            return None
        return sorted(candidates, key=lambda r: (r.effective_at, r.rule_id))[-1]

    chosen = (pick([r for r in active if r.purpose == purpose])
              or pick([r for r in active if r.purpose is None]))
    if chosen is None:
        raise InvalidHistory(
            f"用途 {purpose!r} 在时点 {at.isoformat()} 找不到可用保质期规则"
            + (f"（版本 {rule_version}）" if rule_version else "（含兜底规则）")
        )
    return chosen


def expiry_datetime(batch: Batch, rule: ShelfRule) -> datetime:
    """失效日 = 生产日期 + shelf_life_days 天，在规则时区的当天 00:00。

    生产日期按日历日附着到规则时区（生产日期的“日”由输入日期字面值给出）。
    """
    tz = ZoneInfo(rule.timezone)
    prod_local = batch.production_date.astimezone(tz).date()
    expiry_day = prod_local + timedelta(days=rule.shelf_life_days)
    return datetime(expiry_day.year, expiry_day.month, expiry_day.day, tzinfo=tz)


@dataclass(frozen=True)
class Eligibility:
    batch_id: str
    ok: bool
    reason: str | None = None
    detail: str = ""
    status: QStatus | None = None
    rule_id: str | None = None
    expiry: datetime | None = None
    balance: int = 0


class Engine:
    """绑定数据集与业务时点的可用性引擎。"""

    def __init__(self, ds: Dataset, at: datetime, rule_version: str | None = None):
        self.ds = ds
        self.at = at
        self.rule_version = rule_version
        self.ledger = build_ledger(ds, at)

    def balance(self, batch_id: str) -> int:
        return self.ledger.balances[batch_id]

    def evaluate(self, batch: Batch, order: Order) -> Eligibility:
        """按顺序核验：用途 → 质量证据 → 召回 → 隔离 → 保质期 → 余量。"""
        bal = self.ledger.balances[batch.batch_id]
        status = latest_status(self.ds, batch.batch_id, self.at)

        if order.purpose not in batch.purposes:
            return Eligibility(batch.batch_id, False, REASON_PURPOSE,
                               f"批次用途 {sorted(batch.purposes)} 不覆盖 {order.purpose!r}",
                               status, balance=bal)

        release = latest_release_evidence(self.ds, batch.batch_id, self.at)
        if status is QStatus.RELEASED and (release is None or not release.evidence_id):
            return Eligibility(batch.batch_id, False, REASON_QUALITY_EVIDENCE,
                               "放行状态但缺失质量证据", status, balance=bal)
        if status is QStatus.RECALLED:
            return Eligibility(batch.batch_id, False, REASON_RECALLED,
                               "召回状态", status, balance=bal)
        if status is QStatus.QUARANTINED:
            return Eligibility(batch.batch_id, False, REASON_QUARANTINED,
                               "隔离状态", status, balance=bal)

        rule = select_rule(self.ds.rules, order.purpose, self.at, self.rule_version)
        expiry = expiry_datetime(batch, rule)
        if self.at >= expiry:  # 失效日当天 00:00 起即过期
            return Eligibility(batch.batch_id, False, REASON_EXPIRED,
                               f"业务时点 {self.at.isoformat()} >= 失效日 {expiry.isoformat()} "
                               f"（规则 {rule.rule_id}，时区 {rule.timezone}）",
                               status, rule.rule_id, expiry, bal)

        if bal < order.qty:
            return Eligibility(batch.batch_id, False, REASON_INSUFFICIENT,
                               f"余量 {bal} < 需求 {order.qty}（不可拆批/部分满足）",
                               status, rule.rule_id, expiry, bal)

        return Eligibility(batch.batch_id, True, None,
                           f"余量 {bal}，失效日 {expiry.isoformat()}",
                           status, rule.rule_id, expiry, bal)

    def candidates(self, order: Order) -> list[Eligibility]:
        """该订单在当前时点对全部批次的可用性判定（含不可用原因，便于独立核验）。"""
        out: list[Eligibility] = []
        for bid in sorted(self.ds.batches):
            out.append(self.evaluate(self.ds.batches[bid], order))
        return out

    def feasible_batches(self, order: Order) -> list[str]:
        return [e.batch_id for e in self.candidates(order) if e.ok]
