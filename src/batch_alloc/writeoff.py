"""拣发核销（实际出库登记）。

业务口径
--------
- 核销单含唯一单号 ``writeoff_id``、方案编号 ``plan_id``、出库时点 ``outbound_at``
  与按订单列明的实际批次/数量行。
- 只接受**当前有效（ACTIVE）方案**中的订单；行内批次必须与该订单在方案中的分配批次
  完全一致；数量为正整数，可分次出库，但同一订单累计出库不得超过其原分配量。
- 一张核销单**整体接受或整体拒绝**：任一行不合法则整单不产生任何扣减。
- 幂等：相同单号且内容（方案/出库时点/行集合的规范哈希）相同的重送，返回首次结果且
  **绝不重复扣减**；单号相同而内容不同，直接拒绝（``WO_IDEMPOTENT_CONFLICT``）。
- 若接受该单会使事件账本在任一时点出现负库存（非法库存历史），整单拒绝。
- 方案撤回后不得再凭其未执行部分办理新核销；已接受的出库作为**实际数量事实**永久保留，
  在后续库存计算与重新规划中恰好计入一次（撤回不抹掉事实）。

并发口径
--------
所有写操作（核销提交、方案发布/撤回/重算）与一致性快照查询都在同一把排他锁内完成
（进程内 ``threading.RLock`` + 跨进程 ``fcntl.flock``），配合 JSON 原子替换写盘：
每个操作都基于锁内读取的一致方案状态与库存结果完成校验和落盘，因此并发的最终结果
等价于某一种完整的先后串行执行顺序——不会超额出库，方案撤回后也不可能再接受新核销。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .engine import build_ledger
from .model import (
    Dataset, InputError, InvalidHistory, QuantityEvent, QEventType,
    parse_dt, to_plain,
)
from .registry import PlanRegistry, RegistryError

# --------------------------------------------------------------------------- #
# 失败原因码
# --------------------------------------------------------------------------- #
WO_PLAN_NOT_FOUND = "WO_PLAN_NOT_FOUND"                 # 方案不存在
WO_PLAN_NOT_ACTIVE = "WO_PLAN_NOT_ACTIVE"               # 方案已撤回/非当前有效
WO_DATASET_MISMATCH = "WO_DATASET_MISMATCH"             # 数据集与方案发布时不一致
WO_LINE_UNKNOWN_ORDER = "WO_LINE_UNKNOWN_ORDER"         # 行订单在数据集中不存在
WO_LINE_NOT_IN_PLAN = "WO_LINE_NOT_IN_PLAN"             # 订单不在当前方案分配中
WO_BATCH_MISMATCH = "WO_BATCH_MISMATCH"                 # 实际批次与方案分配批次不一致
WO_QTY_INVALID = "WO_QTY_INVALID"                       # 数量非正整数
WO_DUPLICATE_LINE = "WO_DUPLICATE_LINE"                 # 同一单据内订单重复
WO_EXCEEDS_ALLOCATED = "WO_EXCEEDS_ALLOCATED"           # 累计出库超过原分配量
WO_NEGATIVE_HISTORY = "WO_NEGATIVE_HISTORY"             # 会造成非法（负）库存历史
WO_IDEMPOTENT_CONFLICT = "WO_IDEMPOTENT_CONFLICT"       # 同单号内容不同
WO_DOC_EMPTY = "WO_DOC_EMPTY"                           # 核销单没有任何行

WO_REASON_TEXT = {
    WO_PLAN_NOT_FOUND: "方案编号不存在，无法据此办理核销",
    WO_PLAN_NOT_ACTIVE: "方案不是当前有效方案（已撤回），其未执行部分不得再办理新核销",
    WO_DATASET_MISMATCH: "提交所用数据集与方案发布时的数据集不一致（名称/输入哈希不符）",
    WO_LINE_UNKNOWN_ORDER: "核销行引用了数据集中不存在的订单",
    WO_LINE_NOT_IN_PLAN: "订单不在当前有效方案的分配中，未分配量不得出库",
    WO_BATCH_MISMATCH: "实际出库批次与方案为该订单分配的批次不一致",
    WO_QTY_INVALID: "出库数量必须为正整数",
    WO_DUPLICATE_LINE: "同一核销单内同一订单出现多行，一张单对每单只能有一行",
    WO_EXCEEDS_ALLOCATED: "分次出库累计量超过该订单原分配量",
    WO_NEGATIVE_HISTORY: "接受本单将使批次账面在某一时点为负，非法库存历史，整单拒绝",
    WO_IDEMPOTENT_CONFLICT: "单号已存在但单据内容不同，幂等冲突，拒绝",
    WO_DOC_EMPTY: "核销单必须至少包含一条出库行",
}

STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"


# --------------------------------------------------------------------------- #
# 领域模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WriteOffLine:
    order_id: str
    batch_id: str
    qty: int


@dataclass(frozen=True)
class WriteOffRequest:
    """一次核销提交的规范化内容。content_hash 覆盖方案/出库时点/全部行。"""
    writeoff_id: str
    plan_id: str
    outbound_at: datetime
    lines: tuple[WriteOffLine, ...]
    content_hash: str


@dataclass
class WriteOffFailure:
    code: str
    detail: str


@dataclass
class PlanSnapshot:
    """接受时刻的方案快照（事实凭证，方案日后撤回也不改变）。"""
    plan_id: str
    status: str
    dataset_name: str
    as_of: str
    rule_version: str | None
    input_hash: str
    allocations: list[dict]  # [{order_id, batch_id, qty}]


@dataclass
class WriteOffRecord:
    writeoff_id: str
    plan_id: str
    status: str                       # ACCEPTED / REJECTED
    outbound_at: datetime
    submitted_at: datetime
    content_hash: str
    lines: list[WriteOffLine]
    failures: list[WriteOffFailure] = field(default_factory=list)
    plan_snapshot: PlanSnapshot | None = None
    duplicate: bool = False           # 重送命中（非首次落盘），仅返回态使用


@dataclass
class WriteOffResult:
    accepted: bool
    duplicate: bool
    record: WriteOffRecord

    @property
    def failures(self) -> list[WriteOffFailure]:
        return self.record.failures


@dataclass
class OrderExecution:
    order_id: str
    allocated_batch: str | None       # 当前有效方案分配批次（未分配为 None）
    allocated_qty: int                # 原分配量
    shipped_qty: int                  # 已接受出库累计（实际事实，可能来自已撤回方案）
    remaining_qty: int                # 剩余可执行量 = 分配量 - 已出库量


@dataclass
class BatchStock:
    batch_id: str
    book_balance: int                 # 事件账本账面（不含核销）
    outbound_qty: int                 # 已接受核销出库合计
    on_hand: int                      # 实际库存 = 账面 - 出库


# --------------------------------------------------------------------------- #
# 解析与规范哈希
# --------------------------------------------------------------------------- #
def _canonical_payload(req_fields: Mapping[str, Any]) -> bytes:
    return json.dumps(req_fields, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def content_hash_of(plan_id: str, outbound_at: datetime,
                    lines: list[WriteOffLine] | tuple[WriteOffLine, ...]) -> str:
    payload = {
        "plan_id": plan_id,
        "outbound_at": outbound_at.isoformat(),
        "lines": sorted(
            ({"order_id": ln.order_id, "batch_id": ln.batch_id, "qty": ln.qty}
             for ln in lines),
            key=lambda x: x["order_id"],
        ),
    }
    return hashlib.sha256(_canonical_payload(payload)).hexdigest()[:16]


def parse_writeoff(raw: Mapping[str, Any]) -> WriteOffRequest:
    """解析并做结构级校验；业务级校验（方案状态/累计量/库存历史）在服务层完成。"""
    if not isinstance(raw, Mapping):
        raise InputError("核销单必须是 JSON 对象")
    wid = raw.get("writeoff_id")
    if not isinstance(wid, str) or not wid.strip():
        raise InputError("核销单缺少非空字符串 writeoff_id")
    plan_id = raw.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise InputError(f"核销单 {wid} 缺少非空字符串 plan_id")
    if "outbound_at" not in raw:
        raise InputError(f"核销单 {wid} 缺少必填字段 outbound_at")
    outbound_at = parse_dt(raw["outbound_at"], f"writeoff {wid}.outbound_at")

    raw_lines = raw.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise InputError(f"核销单 {wid} 的 lines 必须是非空数组")
    lines: list[WriteOffLine] = []
    for i, ln in enumerate(raw_lines):
        if not isinstance(ln, Mapping):
            raise InputError(f"核销单 {wid} 第 {i} 行必须是对象")
        oid = ln.get("order_id")
        bid = ln.get("batch_id")
        qty = ln.get("qty")
        if not isinstance(oid, str) or not oid.strip():
            raise InputError(f"核销单 {wid} 第 {i} 行缺少 order_id")
        if not isinstance(bid, str) or not bid.strip():
            raise InputError(f"核销单 {wid} 第 {i} 行缺少 batch_id")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            # 结构阶段直接拦截非正整数，服务层仍保留防御性校验
            raise InputError(f"核销单 {wid} 行 {oid} 数量必须为正整数，实际 {qty!r}")
        # 同一单据内的重复订单不在此拦截，交由服务层给出领域原因码 WO_DUPLICATE_LINE
        lines.append(WriteOffLine(oid, bid, qty))
    return WriteOffRequest(
        writeoff_id=wid, plan_id=plan_id, outbound_at=outbound_at,
        lines=tuple(lines), content_hash=content_hash_of(plan_id, outbound_at, lines),
    )


def load_writeoff(path: str) -> WriteOffRequest:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_writeoff(json.load(fh))


# --------------------------------------------------------------------------- #
# 已接受出库 => 合成 ISSUE 事件（实际数量事实，恰好计入一次）
# --------------------------------------------------------------------------- #
def dataset_with_outbounds(ds: Dataset,
                           records: list[WriteOffRecord],
                           as_of: datetime | None = None) -> Dataset:
    """把已接受核销按出库时点生成为合成 ISSUE 事件，返回新数据集。

    - 事件身份 ``WRITEOFF:<单号>:<批次>`` 天然按单号去重，重放/重算不会重复计数；
    - 同一单据同一批次的多行合并为一笔 ISSUE；
    - 沿用 build_ledger 的时序过账与负库存检查，非法历史会抛 InvalidHistory；
    - ``as_of`` 给定时（重算/规划口径），已接受出库是**实际数量事实**，即使其出库时点
      晚于方案业务时点也必须计入，故把合成事件生效时点钳制到 ``min(出库时点, as_of)``，
      保证在该业务时点的账本中可见；库存时点视图不传 as_of，保留真实出库时点；
    - 保留原 input_hash（核销事实不是输入数据集的一部分，单独留痕）。
    """
    extra: list[QuantityEvent] = []
    for rec in records:
        if rec.status != STATUS_ACCEPTED:
            continue
        effective = rec.outbound_at
        if as_of is not None and effective > as_of:
            effective = as_of
        per_batch: dict[str, int] = {}
        for ln in rec.lines:
            per_batch[ln.batch_id] = per_batch.get(ln.batch_id, 0) + ln.qty
        for bid, qty in sorted(per_batch.items()):
            extra.append(QuantityEvent(
                event_id=f"WRITEOFF:{rec.writeoff_id}:{bid}",
                batch_id=bid, etype=QEventType.ISSUE, qty=qty,
                effective_at=effective, note=f"writeoff {rec.writeoff_id}",
            ))
    return Dataset(
        name=ds.name, batches=ds.batches,
        quantity_events=list(ds.quantity_events) + extra,
        status_events=ds.status_events, rules=ds.rules, orders=ds.orders,
        input_hash=ds.input_hash,
    )


def _far_future(records: list[WriteOffRecord], ds: Dataset) -> datetime:
    times = [e.effective_at for e in ds.quantity_events]
    times += [e.effective_at for e in ds.status_events]
    times += [r.outbound_at for r in records]
    return max(times) + timedelta(seconds=1) if times else datetime(2999, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 核销存储（原子 JSON + 进程内/跨进程排他锁）
# --------------------------------------------------------------------------- #
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _process_lock(path: str) -> threading.RLock:
    key = os.path.realpath(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


class WriteOffStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._rlock = _process_lock(path)
        self._lock_path = path + ".lock"
        if not os.path.exists(path):
            self._write({"writeoffs": []})

    def _read(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def locked(self):
        """排他临界区：同进程 RLock（可重入）+ 跨进程 flock。

        两层锁都在 finally 中释放，临界区抛异常也不会遗留锁导致死锁。
        """
        self._rlock.acquire()
        try:
            with open(self._lock_path, "a+", encoding="utf-8") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        finally:
            self._rlock.release()

    def all_records_raw(self) -> list[dict]:
        return self._read()["writeoffs"]

    def upsert(self, record: WriteOffRecord) -> None:
        data = self._read()
        data["writeoffs"] = [r for r in data["writeoffs"]
                             if r["writeoff_id"] != record.writeoff_id]
        data["writeoffs"].append(to_plain(record))
        self._write(data)


def _record_from_raw(raw: dict) -> WriteOffRecord:
    snapshot = None
    if raw.get("plan_snapshot"):
        snapshot = PlanSnapshot(**raw["plan_snapshot"])
    return WriteOffRecord(
        writeoff_id=raw["writeoff_id"], plan_id=raw["plan_id"],
        status=raw["status"],
        outbound_at=parse_dt(raw["outbound_at"], "record.outbound_at"),
        submitted_at=parse_dt(raw["submitted_at"], "record.submitted_at"),
        content_hash=raw["content_hash"],
        lines=[WriteOffLine(**ln) for ln in raw["lines"]],
        failures=[WriteOffFailure(**f) for f in raw.get("failures", [])],
        plan_snapshot=snapshot,
    )


# --------------------------------------------------------------------------- #
# 仓库服务：核销 + 方案重算在同一串行化点上完成
# --------------------------------------------------------------------------- #
class WarehouseService:
    def __init__(self, registry_path: str, store_path: str | None = None):
        self.registry = PlanRegistry(registry_path)
        if store_path is None:
            store_path = os.path.join(os.path.dirname(os.path.abspath(registry_path)),
                                      "writeoffs.json")
        self.store = WriteOffStore(store_path)

    # -- 内部工具（调用方须持锁） ------------------------------------------- #
    def _records(self) -> list[WriteOffRecord]:
        return [_record_from_raw(r) for r in self.store.all_records_raw()]

    def _accepted_records(self, dataset_name: str | None = None) -> list[WriteOffRecord]:
        out = []
        for rec in self._records():
            if rec.status != STATUS_ACCEPTED:
                continue
            if dataset_name is not None and rec.plan_snapshot is not None \
                    and rec.plan_snapshot.dataset_name != dataset_name:
                continue
            out.append(rec)
        return out

    def _shipped_by_order(self, dataset_name: str) -> dict[str, int]:
        totals: dict[str, int] = {}
        for rec in self._accepted_records(dataset_name):
            for ln in rec.lines:
                totals[ln.order_id] = totals.get(ln.order_id, 0) + ln.qty
        return totals

    def effective_dataset(self, ds: Dataset,
                          as_of: datetime | None = None) -> Dataset:
        """加入全部已接受出库事实后的数据集（库存计算/重算的统一入口）。

        重算/规划传 as_of：合成出库事件钳制到该时点，保证事实在账本中可见；
        库存时点视图不传 as_of：保留真实出库时点，由账本按时点过滤。
        """
        return dataset_with_outbounds(ds, self._accepted_records(ds.name), as_of)

    # -- 核销提交 ------------------------------------------------------------ #
    def submit_writeoff(self, ds: Dataset, req: WriteOffRequest,
                        submitted_at: datetime) -> WriteOffResult:
        with self.store.locked():
            # 1) 幂等处理优先：单号已存在时，内容同 => 回放首次结果；内容异 => 拒绝
            for existing_raw in self.store.all_records_raw():
                if existing_raw["writeoff_id"] != req.writeoff_id:
                    continue
                existing = _record_from_raw(existing_raw)
                if existing.content_hash != req.content_hash:
                    conflict = WriteOffRecord(
                        writeoff_id=req.writeoff_id, plan_id=req.plan_id,
                        status=STATUS_REJECTED, outbound_at=req.outbound_at,
                        submitted_at=submitted_at, content_hash=req.content_hash,
                        lines=list(req.lines),
                        failures=[WriteOffFailure(
                            WO_IDEMPOTENT_CONFLICT,
                            f"原内容哈希 {existing.content_hash} != 本次 {req.content_hash}")],
                    )
                    return WriteOffResult(False, False, conflict)
                existing.duplicate = True
                return WriteOffResult(existing.status == STATUS_ACCEPTED, True, existing)

            failures: list[WriteOffFailure] = []

            # 2) 方案必须存在且当前有效
            plan_rec = None
            try:
                plan_rec = self.registry.get(req.plan_id)
            except RegistryError:
                failures.append(WriteOffFailure(
                    WO_PLAN_NOT_FOUND, WO_REASON_TEXT[WO_PLAN_NOT_FOUND]))
            if plan_rec is not None and plan_rec["status"] != "ACTIVE":
                failures.append(WriteOffFailure(
                    WO_PLAN_NOT_ACTIVE,
                    f"方案 {req.plan_id} 状态={plan_rec['status']}；"
                    + WO_REASON_TEXT[WO_PLAN_NOT_ACTIVE]))

            # 3) 数据集与方案发布口径一致
            plan_result = plan_rec["result"] if plan_rec else None
            if plan_result is not None and (
                    plan_result["dataset_name"] != ds.name
                    or plan_result.get("input_hash") != ds.input_hash):
                failures.append(WriteOffFailure(
                    WO_DATASET_MISMATCH,
                    f"方案数据集={plan_result['dataset_name']}/"
                    f"{plan_result.get('input_hash')}，提交数据集={ds.name}/{ds.input_hash}"))

            # 4) 行级结构核验
            order_map = {o.order_id: o for o in ds.orders}
            alloc_map: dict[str, dict] = {}
            if plan_result is not None:
                alloc_map = {a["order_id"]: a for a in plan_result["allocations"]}

            structural_ok = True
            seen: set[str] = set()
            for ln in req.lines:
                if ln.order_id in seen:
                    failures.append(WriteOffFailure(
                        WO_DUPLICATE_LINE,
                        f"订单 {ln.order_id} 在单 {req.writeoff_id} 内重复出现"))
                    structural_ok = False
                seen.add(ln.order_id)
                if not isinstance(ln.qty, int) or isinstance(ln.qty, bool) or ln.qty <= 0:
                    failures.append(WriteOffFailure(
                        WO_QTY_INVALID, f"订单 {ln.order_id} 数量 {ln.qty!r}"))
                    structural_ok = False
                if ln.order_id not in order_map:
                    failures.append(WriteOffFailure(
                        WO_LINE_UNKNOWN_ORDER, f"订单 {ln.order_id} 不在数据集 {ds.name} 中"))
                    structural_ok = False
                    continue
                alloc = alloc_map.get(ln.order_id)
                if alloc is None:
                    failures.append(WriteOffFailure(
                        WO_LINE_NOT_IN_PLAN,
                        f"订单 {ln.order_id} 不在方案 {req.plan_id} 的分配中"))
                    structural_ok = False
                    continue
                if alloc["batch_id"] != ln.batch_id:
                    failures.append(WriteOffFailure(
                        WO_BATCH_MISMATCH,
                        f"订单 {ln.order_id} 方案批次={alloc['batch_id']}，"
                        f"实际批次={ln.batch_id}"))
                    structural_ok = False

            # 5) 累计出库不得超过原分配量（按订单汇总本单后与历史累计比较）
            shipped = self._shipped_by_order(ds.name)
            doc_qty: dict[str, int] = {}
            for ln in req.lines:
                doc_qty[ln.order_id] = doc_qty.get(ln.order_id, 0) + ln.qty
            for oid, q in doc_qty.items():
                alloc = alloc_map.get(oid)
                if alloc is None:
                    continue
                total = shipped.get(oid, 0) + q
                if total > alloc["qty"]:
                    failures.append(WriteOffFailure(
                        WO_EXCEEDS_ALLOCATED,
                        f"订单 {oid} 原分配 {alloc['qty']}，已出库 {shipped.get(oid, 0)}，"
                        f"本单 {q}，累计 {total} 超量"))

            # 6) 非法库存历史：把历史已接受出库 + 本单合成 ISSUE 全量时序过账
            if structural_ok:
                tentative = WriteOffRecord(
                    writeoff_id=req.writeoff_id, plan_id=req.plan_id,
                    status=STATUS_ACCEPTED, outbound_at=req.outbound_at,
                    submitted_at=submitted_at, content_hash=req.content_hash,
                    lines=list(req.lines),
                )
                aug_ds = dataset_with_outbounds(
                    ds, self._accepted_records(ds.name) + [tentative])
                try:
                    build_ledger(aug_ds, _far_future([tentative], aug_ds))
                except InvalidHistory as exc:
                    failures.append(WriteOffFailure(WO_NEGATIVE_HISTORY, str(exc)))

            # 7) 整单接受或拒绝，并落盘（拒绝也留痕，含原因）
            if failures:
                record = WriteOffRecord(
                    writeoff_id=req.writeoff_id, plan_id=req.plan_id,
                    status=STATUS_REJECTED, outbound_at=req.outbound_at,
                    submitted_at=submitted_at, content_hash=req.content_hash,
                    lines=list(req.lines), failures=failures)
                self.store.upsert(record)
                return WriteOffResult(False, False, record)

            snapshot = PlanSnapshot(
                plan_id=plan_rec["plan_id"], status=plan_rec["status"],
                dataset_name=plan_result["dataset_name"], as_of=plan_result["as_of"],
                rule_version=plan_result.get("rule_version"),
                input_hash=plan_result.get("input_hash", ""),
                allocations=[dict(a) for a in plan_result["allocations"]],
            )
            record = WriteOffRecord(
                writeoff_id=req.writeoff_id, plan_id=req.plan_id,
                status=STATUS_ACCEPTED, outbound_at=req.outbound_at,
                submitted_at=submitted_at, content_hash=req.content_hash,
                lines=list(req.lines), plan_snapshot=snapshot)
            self.store.upsert(record)
            return WriteOffResult(True, False, record)

    # -- 方案发布/重算（持同一把锁，杜绝与核销交错） ------------------------- #
    def publish_plan(self, ds: Dataset, at: datetime, plan_id: str,
                     rule_version: str | None, created_at: datetime,
                     verify: bool = True):
        """在锁内基于含出库事实的数据集求解并发布方案。"""
        from .planner import solve
        from .registry import verify_plan
        with self.store.locked():
            aug = self.effective_dataset(ds, at)
            from .engine import Engine
            engine = Engine(aug, at, rule_version)
            result = solve(engine, plan_id, rule_version, ds.input_hash)
            if verify:
                verify_plan(aug, result, at, rule_version, brute_force=False)
            self.registry.publish(result, created_at)
            return result

    def recalc(self, ds: Dataset, at: datetime, new_plan_id: str,
               rule_version: str | None, created_at: datetime,
               verify: bool = True):
        """重算在核销同一把锁内完成：先撤回旧 ACTIVE 再发布，出库事实恰好计入一次。"""
        with self.store.locked():
            aug = self.effective_dataset(ds, at)
            return self.registry.recalc(aug, at, new_plan_id, rule_version,
                                        created_at, verify=verify)

    def withdraw(self, plan_id: str, at: datetime) -> None:
        with self.store.locked():
            self.registry.withdraw(plan_id, at)

    # -- 查询（快照一致性读） ------------------------------------------------ #
    def list_writeoffs(self, status: str | None = None) -> list[WriteOffRecord]:
        with self.store.locked():
            recs = self._records()
        if status is not None:
            recs = [r for r in recs if r.status == status]
        return recs

    def order_execution(self, ds: Dataset, plan_id: str | None = None
                        ) -> list[OrderExecution]:
        """订单已出库量与剩余可执行量（基于当前有效方案 + 全部已接受出库事实）。"""
        with self.store.locked():
            shipped = self._shipped_by_order(ds.name)
            candidates = [p for p in self.registry.list_plans()
                          if p["result"]["dataset_name"] == ds.name]
            if plan_id is None:
                active = [p for p in candidates if p["status"] == "ACTIVE"]
                if len(active) > 1:
                    raise RegistryError(
                        "数据集存在多个不同时点的 ACTIVE 方案，请显式指定 --plan-id")
                plan_rec = active[0] if active else None
            else:
                plan_rec = next((p for p in candidates if p["plan_id"] == plan_id), None)
                if plan_rec is None:
                    raise RegistryError(f"方案不存在: {plan_id}")

            alloc_map: dict[str, dict] = {}
            if plan_rec is not None:
                alloc_map = {a["order_id"]: a
                             for a in plan_rec["result"]["allocations"]}
            rows: list[OrderExecution] = []
            order_ids = sorted({o.order_id for o in ds.orders} | set(shipped))
            for oid in order_ids:
                alloc = alloc_map.get(oid)
                done = shipped.get(oid, 0)
                allocated = alloc["qty"] if alloc else 0
                remaining = max(0, allocated - done)
                rows.append(OrderExecution(
                    order_id=oid,
                    allocated_batch=alloc["batch_id"] if alloc else None,
                    allocated_qty=allocated, shipped_qty=done,
                    remaining_qty=remaining))
            return rows

    def stock_view(self, ds: Dataset, at: datetime | None = None) -> list[BatchStock]:
        """批次账面、已出库与实际库存；at 给定时只计入 effective_at <= at 的事实。"""
        from .engine import Engine
        with self.store.locked():
            accepted = self._accepted_records(ds.name)
            if at is not None:
                accepted = [r for r in accepted if r.outbound_at <= at]
            aug = dataset_with_outbounds(ds, accepted)
            view_at = at or _far_future(accepted, ds)
            base_engine = Engine(ds, view_at)
            aug_engine = Engine(aug, view_at)
            rows = []
            for bid in sorted(ds.batches):
                book = base_engine.balance(bid)
                on_hand = aug_engine.balance(bid)
                rows.append(BatchStock(bid, book, book - on_hand, on_hand))
            return rows
