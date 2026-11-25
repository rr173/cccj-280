"""只读召回清单：来源追溯 + 加工关联 + 退回关联。

本模块**只读**：不修改数据集、核销存储或方案登记册，只在内存中重建一份
带来源（provenance）的数量账本，输出截至业务时点的召回清单。

业务口径
--------
1. **数量按事件账本顺序过账**：同一时点按事件 ID 排序；已接受核销按
   ``WRITEOFF:<单号>:<批次>`` 作为合成 ISSUE 事件，沿用核销入账的事件身份。
   同一单据同批多条订单行按订单 ID 顺序取货。
2. **同批先进先出**：领用、报废、核销出库都只消耗“当时已经入库且尚未消耗”的
   最早货物；不会把同批后来入库的货物算进较早的发出记录。
3. **召回起点**：给定的原料入库（RECEIPT）事件，必须截至时点已经发生，
   且不得是任何加工关联的产出。
4. **加工关联**：一组领用（ISSUE）事件与一组产成品入库（RECEIPT）事件标识同一次
   加工。每个事件至多归属一次加工；按账本顺序全部领用必须早于全部产出。
   关联本身不增减库存，只把被领用货物所带的召回来源传递给该次加工的全部产出。
   只要某次加工**实际领用**过某召回来源的货物（哪怕一单位），它的全部产出都
   视为受该来源影响；未被领用的库存和更早的产出不受这次加工影响。
   产出携带来源**集合**，同一来源经多路加工汇合只记一次。
5. **退回关联**：每笔按现有规则入账（未拆封且验收合格）的退回都要指向账本顺序中
   已经发生的一条**已接受**核销订单行，且批次相同。分次退回按该行当初取货的
   先后认定收回的是哪部分，累计不能超过该行数量；收回货物保留原有来源，
   在退回时点重新进入仓库库存。未入账的退回不计为收回。
6. **错误即停**：缺少必需的退回关联，或发现关联无效、身份冲突、超退、负余额等，
   抛 ``RecallError`` 且不输出不完整的清单。
7. **数量规模**：全程只按区间与数量运算，不按单件展开；十亿级数量同样可算。

输出（JSON）::

    {
      "as_of": ..., "dataset": ..., "recall_sources": [...],
      "affected_orders": [                      # 已出库订单尚未收回的受影响货物
        {"writeoff_id","order_id","batch_id","outbound_at",
         "qty","by_source":{...},
         "unrecovered_qty","unrecovered_by_source":{...}}
      ],
      "affected_stock": [                       # 仍有受影响库存的批次
        {"batch_id","on_hand","affected_qty","by_source":{...}}
      ],
      "in_transit": [...],                      # 已领用、尚未产出的受影响数量
      "recovered": [...],                       # 已收回并重新入库的受影响数量
      "totals": {"affected_stock": N, "affected_unrecovered": N,
                 "affected_in_transit": N, "affected_outstanding": N,
                 "affected_shipped": N, "affected_recovered": N}
    }

``by_source`` 只列数量为正的召回来源；``qty`` / ``affected_qty`` 是
**同一货物只算一次**的受影响总量，不是各来源之和（多来源汇合时两者可能不同）。

命令行：``python3 -I src/cli.py recall --input ... --as-of ... --recall ...``，
只读，不写任何文件；发现错误退出码 ``3``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .model import Dataset, InputError, InvalidHistory, QEventType, parse_dt
from .writeoff import STATUS_ACCEPTED, WriteOffRecord, _record_from_raw


# --------------------------------------------------------------------------- #
# 错误码（一律“报错而不交出不完整清单”）
# --------------------------------------------------------------------------- #
RC_MISSING_RETURN_LINK = "RC_MISSING_RETURN_LINK"
RC_UNKNOWN_EVENT = "RC_UNKNOWN_EVENT"
RC_DUPLICATE_EVENT = "RC_DUPLICATE_EVENT"
RC_EVENT_CONFLICT = "RC_EVENT_CONFLICT"
RC_INVALID_LINK = "RC_INVALID_LINK"
RC_EVENT_MISMATCH = "RC_EVENT_MISMATCH"
RC_RETURN_NO_TARGET = "RC_RETURN_NO_TARGET"
RC_RETURN_BATCH_MISMATCH = "RC_RETURN_BATCH_MISMATCH"
RC_RETURN_OVER = "RC_RETURN_OVER"
RC_NEGATIVE_BALANCE = "RC_NEGATIVE_BALANCE"

RC_REASON_TEXT = {
    RC_MISSING_RETURN_LINK: "存在按规则入账的退回事件缺少退回关联（无法认定收回了哪部分货物）",
    RC_UNKNOWN_EVENT: "引用了数据集中不存在的数量事件",
    RC_DUPLICATE_EVENT: "同一事件被多个关联重复引用（事件身份冲突）",
    RC_EVENT_CONFLICT: "事件 ID 在数据集内重复或与合成核销事件冲突",
    RC_INVALID_LINK: "关联引用了不存在的事件 ID 或结构不成立（关联无效）",
    RC_EVENT_MISMATCH: "关联引用的事件类型或批次不符合要求",
    RC_RETURN_NO_TARGET: "退回指向账本顺序中尚未发生或未被接受的核销订单行",
    RC_RETURN_BATCH_MISMATCH: "退回批次与所指核销订单行的批次不一致",
    RC_RETURN_OVER: "分段退回累计超过所指核销订单行的数量（超退）",
    RC_NEGATIVE_BALANCE: "按账本顺序过账出现负余额（非法历史）",
}


class RecallError(InvalidHistory):
    """召回清单无法完整、可靠地给出。携带错误码与明细。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        text = RC_REASON_TEXT.get(code, code)
        super().__init__(f"[{code}] {text}" + (f"：{detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# 输入：加工关联 / 退回关联 / 召回起点
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProcessLink:
    """一次加工：若干领用事件 + 若干产成品入库事件（均可多笔）。"""
    link_id: str
    issue_events: tuple[str, ...]
    output_events: tuple[str, ...]


@dataclass(frozen=True)
class ReturnLink:
    """一笔已入账退回收回的是哪一条核销订单行（同批、账本顺序已发生、已接受）。"""
    return_event_id: str
    writeoff_id: str
    order_id: str
    batch_id: str


@dataclass(frozen=True)
class RecallInput:
    recall_source_event_ids: tuple[str, ...]
    processes: tuple[ProcessLink, ...] = ()
    returns: tuple[ReturnLink, ...] = ()


def _require_str_list(raw: Any, ctx: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise InputError(f"{ctx} 必须是非空字符串数组")
    out = []
    for x in raw:
        if not isinstance(x, str) or not x.strip():
            raise InputError(f"{ctx} 含非字符串/空元素: {x!r}")
        out.append(x)
    return tuple(out)


def parse_recall_input(raw: Mapping[str, Any]) -> RecallInput:
    """解析召回输入结构（加工关联 / 退回关联 / 待召回原料入库事件 ID）。"""
    if not isinstance(raw, Mapping):
        raise InputError("召回输入必须是 JSON 对象")
    sources = _require_str_list(
        raw.get("recall_source_event_ids"), "recall_source_event_ids")

    procs: list[ProcessLink] = []
    seen_link: set[str] = set()
    for i, p in enumerate(raw.get("processes", [])):
        if not isinstance(p, Mapping):
            raise InputError(f"processes[{i}] 必须是对象")
        lid = p.get("link_id", f"LINK-{i}")
        if not isinstance(lid, str) or not lid.strip():
            raise InputError(f"processes[{i}] link_id 必须是非空字符串")
        if lid in seen_link:
            raise RecallError(RC_EVENT_CONFLICT, f"加工关联 link_id 重复: {lid}")
        seen_link.add(lid)
        procs.append(ProcessLink(
            link_id=lid,
            issue_events=_require_str_list(p.get("issue_events"),
                                           f"process {lid}.issue_events"),
            output_events=_require_str_list(p.get("output_events"),
                                            f"process {lid}.output_events"),
        ))

    rets: list[ReturnLink] = []
    seen_ret: set[str] = set()
    for i, r in enumerate(raw.get("returns", [])):
        if not isinstance(r, Mapping):
            raise InputError(f"returns[{i}] 必须是对象")
        rid = r.get("return_event_id")
        wid = r.get("writeoff_id")
        oid = r.get("order_id")
        bid = r.get("batch_id")
        for name, val in (("return_event_id", rid), ("writeoff_id", wid),
                          ("order_id", oid), ("batch_id", bid)):
            if not isinstance(val, str) or not val.strip():
                raise InputError(f"returns[{i}] 缺少非空字符串 {name}")
        if rid in seen_ret:
            raise RecallError(RC_EVENT_CONFLICT, f"退回关联重复引用事件 {rid}")
        seen_ret.add(rid)
        rets.append(ReturnLink(rid, wid, oid, bid))

    return RecallInput(sources, tuple(procs), tuple(rets))


def load_recall_input(path: str) -> RecallInput:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_recall_input(json.load(fh))


# --------------------------------------------------------------------------- #
# 来源数量（{召回来源: 数量}）与批次 FIFO 账本
# --------------------------------------------------------------------------- #
SourceMap = dict[str, int]
Group = tuple[frozenset[str], int]   # (命中的召回来源集合, 数量)


def _copy(src: Mapping[str, int]) -> SourceMap:
    return {k: v for k, v in src.items() if v}


def _add(dst: SourceMap, src: Mapping[str, int], sign: int = 1) -> None:
    for k, v in src.items():
        if not v:
            continue
        nv = dst.get(k, 0) + sign * v
        if nv:
            dst[k] = nv
        else:
            dst.pop(k, None)


def _by_source(groups: list[Group]) -> SourceMap:
    """按来源归集数量：同一段货物命中的每个来源各记一次该段数量。"""
    out: SourceMap = {}
    for sources, qty in groups:
        for s in sources:
            out[s] = out.get(s, 0) + qty
    return out


def _affected_qty(groups: list[Group]) -> int:
    """受影响总量：命中任一召回来源的货物只算一次（与来源数无关）。"""
    return sum(qty for sources, qty in groups if sources)


# --------------------------------------------------------------------------- #
# 区间式来源账本：同批 FIFO，数量用区间表示，绝不按单件展开
# --------------------------------------------------------------------------- #
@dataclass
class _Lot:
    """同批次、同一次入库/退回形成的一段货物；groups 之和等于本段数量。"""
    groups: list[Group]
    received_at: datetime
    event_id: str

    @property
    def qty(self) -> int:
        return sum(q for _, q in self.groups)


class BatchLots:
    """单个批次的货物队列（队首最早）。每段记录其召回来源集合。"""

    def __init__(self) -> None:
        self.lots: list[_Lot] = []

    def on_hand(self) -> int:
        return sum(lot.qty for lot in self.lots)

    def append(self, groups: list[Group], received_at: datetime,
               event_id: str) -> None:
        groups = [(frozenset(s), q) for s, q in groups if q > 0]
        if not groups:
            raise RecallError(RC_NEGATIVE_BALANCE, f"入库数量必须为正: {event_id}")
        self.lots.append(_Lot(groups, received_at, event_id))

    def take(self, qty: int) -> list[Group]:
        """按 FIFO 取 qty 件，返回取出的 [(来源集合, 件数), ...]（保持先后）。"""
        if qty < 0:
            raise RecallError(RC_NEGATIVE_BALANCE, f"取货数量为负: {qty}")
        out: list[Group] = []
        left = qty
        while left > 0 and self.lots:
            lot = self.lots[0]
            while left > 0 and lot.groups:
                sources, gqty = lot.groups[0]
                use = gqty if gqty < left else left
                out.append((sources, use))
                left -= use
                if use == gqty:
                    lot.groups.pop(0)
                else:
                    lot.groups[0] = (sources, gqty - use)
            if not lot.groups:
                self.lots.pop(0)
        if left > 0:
            raise RecallError(
                RC_NEGATIVE_BALANCE,
                f"批次余额不足，尚缺 {left} 件（账本顺序不允许负库存）")
        return out

    def affected(self) -> tuple[int, SourceMap]:
        """在库受影响数量（只算一次）及各召回来源对应数量（可能重复计入同段）。"""
        groups = [g for lot in self.lots for g in lot.groups]
        return _affected_qty(groups), _by_source(groups)


# --------------------------------------------------------------------------- #
# 召回分析
# --------------------------------------------------------------------------- #
@dataclass
class _Entry:
    """账本中的一条数量事件（数据集事件或已接受核销的合成 ISSUE）。"""
    event_id: str
    batch_id: str
    etype: QEventType
    qty: int
    effective_at: datetime
    counted: bool = True
    record: WriteOffRecord | None = None   # 合成核销事件所属单据
    process_id: str | None = None          # 归属的加工关联


@dataclass
class _LineState:
    """一条已接受核销订单行的取货与退回状态。

    pieces 是尚未收回的取货段（队首最早）；每段是 [(来源集合, 件数), ...]。
    分次退回按该行当初取货的先后从 pieces 队首扣减。
    """
    writeoff_id: str
    order_id: str
    batch_id: str
    outbound_at: datetime
    pieces: list[list[Group]]
    ever_groups: list[Group]               # 原始取货全量（用于“曾受影响”口径）
    recovered_qty: int = 0

    def remaining_qty(self) -> int:
        return sum(q for piece in self.pieces for _, q in piece)

    def remaining_groups(self) -> list[Group]:
        return [g for piece in self.pieces for g in piece]


def _event_sort_key(e) -> tuple:
    return (e.effective_at, e.event_id)


def recall_report(ds: Dataset, records: list[WriteOffRecord],
                  recall: RecallInput, as_of: datetime) -> dict:
    """只读生成召回清单。任何不完整/冲突/非法状态都抛 RecallError。"""
    # ---- 1) 数据集事件索引与身份冲突检查 -------------------------------- #
    dataset_events: dict[str, Any] = {}
    for ev in ds.quantity_events:
        if ev.event_id in dataset_events:
            raise RecallError(RC_EVENT_CONFLICT, f"数量事件 ID 重复: {ev.event_id}")
        dataset_events[ev.event_id] = ev

    # 账本接受的退回（现有规则：未拆封且验收合格）
    accounted_returns = {
        ev.event_id for ev in ds.quantity_events
        if ev.etype is QEventType.RETURN and bool(ev.sealed) and bool(ev.accepted)
        and ev.effective_at <= as_of
    }

    # ---- 2) 截至时点已接受的核销（不论方案是否已撤回） ------------------- #
    accepted = []
    for r in records:
        if r.status != STATUS_ACCEPTED or r.outbound_at > as_of:
            continue
        if r.plan_snapshot is not None and r.plan_snapshot.dataset_name != ds.name:
            continue    # 只计本数据集的核销记录
        accepted.append(r)
    accepted.sort(key=lambda r: (r.outbound_at, r.writeoff_id))

    synth_by_id: dict[str, _Entry] = {}
    for rec in accepted:
        per_batch: dict[str, int] = {}
        for ln in rec.lines:
            per_batch[ln.batch_id] = per_batch.get(ln.batch_id, 0) + ln.qty
        for bid, qty in sorted(per_batch.items()):
            eid = f"WRITEOFF:{rec.writeoff_id}:{bid}"
            if eid in dataset_events or eid in synth_by_id:
                raise RecallError(RC_EVENT_CONFLICT, f"事件身份冲突: {eid}")
            synth_by_id[eid] = _Entry(eid, bid, QEventType.ISSUE, qty,
                                      rec.outbound_at, record=rec)

    # ---- 3) 加工关联校验 ------------------------------------------------ #
    all_events: dict[str, _Entry] = {}
    for ev in ds.quantity_events:
        all_events[ev.event_id] = _Entry(
            ev.event_id, ev.batch_id, ev.etype, ev.qty, ev.effective_at,
            counted=(ev.etype is not QEventType.RETURN
                     or (bool(ev.sealed) and bool(ev.accepted))))
    all_events.update(synth_by_id)

    event_process: dict[str, str] = {}
    processes: dict[str, ProcessLink] = {}
    for proc in recall.processes:
        if proc.link_id in processes:
            raise RecallError(RC_EVENT_CONFLICT, f"加工关联 link_id 重复: {proc.link_id}")
        processes[proc.link_id] = proc
        for eid in proc.issue_events + proc.output_events:
            if eid not in all_events:
                raise RecallError(RC_INVALID_LINK,
                                  f"加工关联 {proc.link_id} 引用未知事件 {eid}")
            if eid in event_process:
                raise RecallError(RC_DUPLICATE_EVENT,
                                  f"事件 {eid} 同时归属加工 {event_process[eid]} 与 "
                                  f"{proc.link_id}")
            event_process[eid] = proc.link_id
        for eid in proc.issue_events:
            if all_events[eid].etype is not QEventType.ISSUE:
                raise RecallError(RC_EVENT_MISMATCH,
                                  f"加工关联 {proc.link_id} 的领用事件 {eid} 不是 ISSUE")
        for eid in proc.output_events:
            if all_events[eid].etype is not QEventType.RECEIPT:
                raise RecallError(RC_EVENT_MISMATCH,
                                  f"加工关联 {proc.link_id} 的产出事件 {eid} 不是 RECEIPT")
        max_issue = max(_event_sort_key(all_events[e]) for e in proc.issue_events)
        min_output = min(_event_sort_key(all_events[e]) for e in proc.output_events)
        if not max_issue < min_output:
            raise RecallError(
                RC_INVALID_LINK,
                f"加工关联 {proc.link_id} 存在领用晚于（或不早于）产出的事件")

    output_to_process = {eid: p.link_id for p in recall.processes
                         for eid in p.output_events}

    # ---- 4) 召回起点校验 ------------------------------------------------ #
    recall_sources: list[str] = []
    seen_src: set[str] = set()
    for sid in recall.recall_source_event_ids:
        if sid in seen_src:
            raise RecallError(RC_EVENT_CONFLICT, f"召回起点重复: {sid}")
        seen_src.add(sid)
        ev = dataset_events.get(sid)
        if ev is None:
            if sid in synth_by_id:
                raise RecallError(RC_EVENT_MISMATCH,
                                  f"召回起点 {sid} 是核销合成事件，不是原料入库")
            raise RecallError(RC_INVALID_LINK, f"召回起点 {sid} 不是数据集中的事件")
        if ev.etype is not QEventType.RECEIPT:
            raise RecallError(RC_EVENT_MISMATCH,
                              f"召回起点 {sid} 不是入库（RECEIPT）事件")
        if sid in output_to_process:
            raise RecallError(RC_EVENT_MISMATCH,
                              f"召回起点 {sid} 是加工产出，不能作为召回起点")
        if ev.effective_at > as_of:
            raise RecallError(RC_EVENT_MISMATCH,
                              f"召回起点 {sid} 在查询时点之后才发生")
        recall_sources.append(sid)
    recall_source_set = set(recall_sources)
    source_order = {s: i for i, s in enumerate(
        sorted(recall_sources, key=lambda x: _event_sort_key(dataset_events[x])))}

    # ---- 5) 退回关联校验（静态部分） ------------------------------------ #
    return_links: dict[str, ReturnLink] = {}
    for rl in recall.returns:
        if rl.return_event_id in return_links:
            raise RecallError(RC_EVENT_CONFLICT, f"退回关联重复: {rl.return_event_id}")
        ev = dataset_events.get(rl.return_event_id)
        if ev is None or ev.etype is not QEventType.RETURN:
            raise RecallError(RC_INVALID_LINK,
                              f"退回关联指向未知或非退回事件 {rl.return_event_id}")
        if ev.batch_id != rl.batch_id:
            raise RecallError(RC_RETURN_BATCH_MISMATCH,
                              f"退回事件 {rl.return_event_id} 批次={ev.batch_id}，"
                              f"关联声明批次={rl.batch_id}")
        # 未入账退回的关联保留但不参与收回（未入账的退回不计为收回）
        return_links[rl.return_event_id] = rl
    missing = sorted(accounted_returns - set(return_links))
    if missing:
        raise RecallError(RC_MISSING_RETURN_LINK,
                          f"以下已入账退回缺少退回关联: {', '.join(missing)}")

    # ---- 6) 按账本顺序过账（事件 + 已接受核销合成 ISSUE） ---------------- #
    entries: list[_Entry] = []
    for ev in ds.quantity_events:
        if ev.effective_at > as_of:
            continue
        entries.append(all_events[ev.event_id])
    entries.extend(e for e in synth_by_id.values() if e.effective_at <= as_of)
    entries.sort(key=_event_sort_key)

    lots: dict[str, BatchLots] = {bid: BatchLots() for bid in ds.batches}
    balances: dict[str, int] = {bid: 0 for bid in ds.batches}
    process_sources: dict[str, set[str]] = {p: set() for p in processes}
    process_inputs: dict[str, list[Group]] = {p: [] for p in processes}
    process_output_seen: dict[str, bool] = {p: False for p in processes}
    lines: dict[tuple[str, str], _LineState] = {}
    recovered_rows: list[dict] = []

    for entry in entries:
        bid = entry.batch_id
        if bid not in lots:
            raise RecallError(RC_INVALID_LINK, f"事件 {entry.event_id} 引用未知批次 {bid}")
        bucket = lots[bid]

        if entry.etype is QEventType.RECEIPT:
            if not entry.counted:
                continue
            if entry.event_id in recall_source_set:
                groups = [(frozenset({entry.event_id}), entry.qty)]
            elif entry.event_id in output_to_process:
                pid = output_to_process[entry.event_id]
                process_output_seen[pid] = True
                srcs = frozenset(process_sources[pid])
                groups = [(srcs, entry.qty)]
            else:
                groups = [(frozenset(), entry.qty)]
            bucket.append(groups, entry.effective_at, entry.event_id)
            balances[bid] += entry.qty
            continue

        if entry.etype is QEventType.ISSUE:
            if entry.record is not None:
                taken = bucket.take(entry.qty)
                balances[bid] -= entry.qty
                if balances[bid] < 0:
                    raise RecallError(RC_NEGATIVE_BALANCE,
                                      f"核销 {entry.event_id} 使批次 {bid} 为负")
                # 同一单据同批多个订单行按订单 ID 顺序取货
                for ln in sorted((ln for ln in entry.record.lines if ln.batch_id == bid),
                                 key=lambda x: x.order_id):
                    piece = _take_prefix(taken, ln.qty)
                    state = _LineState(
                        writeoff_id=entry.record.writeoff_id,
                        order_id=ln.order_id, batch_id=ln.batch_id,
                        outbound_at=entry.record.outbound_at,
                        pieces=[piece],
                        ever_groups=[g for g in piece])
                    lines[(entry.record.writeoff_id, ln.order_id)] = state
                continue
            taken = bucket.take(entry.qty)
            balances[bid] -= entry.qty
            if balances[bid] < 0:
                raise RecallError(RC_NEGATIVE_BALANCE, f"事件 {entry.event_id} 使批次 {bid} 为负")
            pid = event_process.get(entry.event_id)
            if pid is not None:
                for srcs, n in taken:
                    process_sources[pid].update(srcs)
                    if srcs:      # 实际领用了召回来源的货物（按段只记一次）
                        process_inputs[pid].append((srcs, n))
            continue

        if entry.etype is QEventType.SCRAP:
            bucket.take(entry.qty)
            balances[bid] -= entry.qty
            if balances[bid] < 0:
                raise RecallError(RC_NEGATIVE_BALANCE, f"事件 {entry.event_id} 使批次 {bid} 为负")
            continue

        if entry.etype is QEventType.RETURN:
            if not entry.counted:
                continue
            link = return_links.get(entry.event_id)
            if link is None:  # 上面已保证 accounted 退回必有 link
                raise RecallError(RC_MISSING_RETURN_LINK, entry.event_id)
            state = lines.get((link.writeoff_id, link.order_id))
            if state is None:
                raise RecallError(
                    RC_RETURN_NO_TARGET,
                    f"退回 {entry.event_id} 指向的核销行 {link.writeoff_id}/"
                    f"{link.order_id} 在账本顺序中尚未发生或未被接受")
            if state.batch_id != entry.batch_id:
                raise RecallError(
                    RC_RETURN_BATCH_MISMATCH,
                    f"退回 {entry.event_id} 批次={entry.batch_id}，"
                    f"目标行 {link.order_id} 批次={state.batch_id}")
            if entry.qty > state.remaining_qty():
                raise RecallError(
                    RC_RETURN_OVER,
                    f"退回 {entry.event_id} 数量 {entry.qty} 超过目标行 "
                    f"{link.writeoff_id}/{link.order_id} 未收回量 "
                    f"{state.remaining_qty()}（原始出库 "
                    f"{sum(q for _, q in state.ever_groups)}，"
                    f"已收回 {state.recovered_qty}）")
            moved = _take_front(state.pieces, entry.qty)
            state.recovered_qty += entry.qty
            bucket.append(moved, entry.effective_at, entry.event_id)
            balances[bid] += entry.qty
            recovered_rows.append({
                "return_event_id": entry.event_id,
                "writeoff_id": link.writeoff_id,
                "order_id": link.order_id,
                "batch_id": bid,
                "qty": _affected_qty(moved),
                "by_source": _by_source(moved),
                "effective_at": entry.effective_at,
            })
            continue

    # ---- 7) 账本一致性自检 ---------------------------------------------- #
    for bid, bucket in lots.items():
        if bucket.on_hand() != balances[bid]:
            raise RecallError(
                RC_NEGATIVE_BALANCE,
                f"批次 {bid} 来源账本 {bucket.on_hand()} 与数量余额 {balances[bid]} 不一致")

    return _assemble(ds, as_of, recall_sources, dataset_events, lines,
                     lots, recovered_rows, processes, process_inputs,
                     process_output_seen, event_process)


def recall_report_from_store(ds: Dataset, store_path: str,
                             recall: RecallInput, as_of: datetime) -> dict:
    """只读从核销存储文件读取记录后生成清单（不加锁、不写盘）。"""
    import json as _json
    import os as _os
    if not _os.path.exists(store_path):
        records: list[WriteOffRecord] = []
    else:
        with open(store_path, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        records = [_record_from_raw(r) for r in data.get("writeoffs", [])]
    return recall_report(ds, records, recall, as_of)


# --------------------------------------------------------------------------- #
# FIFO 小工具
# --------------------------------------------------------------------------- #
def _take_prefix(taken: list[Group], qty: int) -> list[Group]:
    """按取货先后**消费** taken 的前 qty 件（同一单据多订单行按订单 ID 顺序取货）。"""
    out: list[Group] = []
    left = qty
    while left > 0 and taken:
        srcs, n = taken[0]
        use = n if n < left else left
        out.append((srcs, use))
        left -= use
        if use == n:
            taken.pop(0)
        else:
            taken[0] = (srcs, n - use)
    if left > 0:
        raise RecallError(RC_NEGATIVE_BALANCE,
                          f"单据取货数量不足，尚缺 {left} 件")
    return out


def _take_front(pieces: list[list[Group]], qty: int) -> list[Group]:
    """从尚未收回的取货段中按当初取货先后取 qty 件（用于分次退回）。"""
    out: list[Group] = []
    left = qty
    while left > 0 and pieces:
        piece = pieces[0]
        while left > 0 and piece:
            srcs, n = piece[0]
            use = n if n < left else left
            out.append((srcs, use))
            left -= use
            if use == n:
                piece.pop(0)
            else:
                piece[0] = (srcs, n - use)
        if not piece:
            pieces.pop(0)
    if left > 0:
        raise RecallError(RC_RETURN_OVER, f"可收回取货段不足，尚缺 {left} 件")
    return out


# --------------------------------------------------------------------------- #
# 清单组装
# --------------------------------------------------------------------------- #
def _assemble(ds: Dataset, as_of: datetime, recall_sources: list[str],
              dataset_events: dict[str, Any],
              lines: dict[tuple[str, str], _LineState],
              lots: dict[str, BatchLots],
              recovered_rows: list[dict],
              processes: dict[str, ProcessLink],
              process_inputs: dict[str, list[Group]],
              process_output_seen: dict[str, bool],
              event_process: dict[str, str]) -> dict:
    source_pos = {s: i for i, s in enumerate(
        sorted(recall_sources, key=lambda x: _event_sort_key(dataset_events[x])))}

    def ordered(m: Mapping[str, int]) -> SourceMap:
        return {s: m[s] for s in sorted(m, key=lambda x: (source_pos.get(x, 1 << 30), x))
                if m[s] > 0}

    affected_orders: list[dict] = []
    total_shipped_affected = 0
    for (wid, oid) in sorted(lines):
        state = lines[(wid, oid)]
        remaining = state.remaining_groups()
        by_src = _by_source(remaining)
        ever = _affected_qty(state.ever_groups)
        total_shipped_affected += ever
        if not by_src:      # 已全部收回：只出现在 recovered，不算“未收回”
            continue
        affected_orders.append({
            "writeoff_id": wid,
            "order_id": oid,
            "batch_id": state.batch_id,
            "outbound_at": state.outbound_at,
            "qty": ever,
            "by_source": ordered(_by_source(state.ever_groups)),
            "unrecovered_qty": _affected_qty(remaining),
            "unrecovered_by_source": ordered(by_src),
        })

    affected_stock: list[dict] = []
    for bid in sorted(lots):
        qty, by_src = lots[bid].affected()
        if not by_src:
            continue
        affected_stock.append({
            "batch_id": bid,
            "on_hand": lots[bid].on_hand(),
            "affected_qty": qty,
            "by_source": ordered(by_src),
        })

    recovered: list[dict] = []
    for row in sorted(recovered_rows,
                      key=lambda r: (r["effective_at"], r["return_event_id"])):
        if not row["by_source"]:
            continue
        recovered.append({
            "return_event_id": row["return_event_id"],
            "writeoff_id": row["writeoff_id"],
            "order_id": row["order_id"],
            "batch_id": row["batch_id"],
            "effective_at": row["effective_at"],
            "qty": _affected_qty([(frozenset(row["by_source"]), row["qty"])]),
            "by_source": ordered(row["by_source"]),
        })

    in_transit: list[dict] = []
    for pid in sorted(processes):
        groups = process_inputs[pid]
        if not groups or process_output_seen[pid]:
            continue    # 已产出（产出已进入库存/订单口径），不再算在途
        in_transit.append({
            "process_id": pid,
            "issue_events": sorted(e for e in processes[pid].issue_events
                                   if event_process.get(e) == pid),
            "qty": _affected_qty(groups),
            "by_source": ordered(_by_source(groups)),
        })

    total_stock = sum(r["affected_qty"] for r in affected_stock)
    total_unrecovered = sum(r["unrecovered_qty"] for r in affected_orders)
    total_ever = total_shipped_affected
    total_recovered = sum(r["qty"] for r in recovered)
    total_in_transit = sum(r["qty"] for r in in_transit)

    return {
        "as_of": as_of,
        "dataset": ds.name,
        "recall_sources": sorted(recall_sources,
                                 key=lambda x: (source_pos[x], x)),
        "affected_orders": affected_orders,
        "affected_stock": affected_stock,
        "in_transit": in_transit,
        "recovered": recovered,
        "totals": {
            # 当前仍受影响的货物（同一件只出现一次）：在库 + 已出库未收回 + 加工中在途
            "affected_stock": total_stock,
            "affected_unrecovered": total_unrecovered,
            "affected_in_transit": total_in_transit,
            "affected_outstanding": (total_stock + total_unrecovered
                                     + total_in_transit),
            # 流量（仅供参考，不计入上面的存量；收回货物已重新计入在库）
            "affected_shipped": total_ever,
            "affected_recovered": total_recovered,
        },
    }
