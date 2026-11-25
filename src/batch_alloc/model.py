"""领域模型、输入解析与校验。

所有时间均为**带时区的绝对时刻**（datetime，tzinfo 必填）。
失效日的计算时区由规则（ShelfRule.timezone）明确给出，不由订单或批次隐含。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
from zoneinfo import ZoneInfo


class AllocError(Exception):
    """本库所有领域异常的基类。"""


class InputError(AllocError):
    """输入数据结构/取值不合法。"""


class InvalidHistory(AllocError):
    """事件历史不合法：负数量、无来源领用/报废、未知批次等。必须拒绝，禁止负库存补方案。"""


class QEventType(str, Enum):
    RECEIPT = "RECEIPT"   # 入库
    ISSUE = "ISSUE"       # 领用（出库）
    RETURN = "RETURN"     # 退回（仅未拆封且验收合格才入账）
    SCRAP = "SCRAP"       # 报废


class QStatus(str, Enum):
    RELEASED = "RELEASED"        # 放行（必须有质量证据）
    QUARANTINED = "QUARANTINED"  # 隔离
    RECALLED = "RECALLED"        # 召回


@dataclass(frozen=True)
class QuantityEvent:
    """数量事件。事件身份即 event_id，同身份只计一次（去重）。"""
    event_id: str
    batch_id: str
    etype: QEventType
    qty: int
    effective_at: datetime
    # 退回专用：未拆封且验收合格才允许入账
    sealed: bool | None = None
    accepted: bool | None = None
    note: str = ""


@dataclass(frozen=True)
class StatusEvent:
    """质量状态事件（放行/隔离/召回）。状态事件只改可用性，永不改账面量。"""
    event_id: str
    batch_id: str
    status: QStatus
    effective_at: datetime
    evidence_id: str | None = None  # 放行必须给出质量证据
    note: str = ""


@dataclass(frozen=True)
class Batch:
    batch_id: str
    production_date: datetime          # 生产日期（当天 00:00，时区由输入给出）
    purposes: frozenset[str]           # 该批次可服务的用途集合
    initial_status: QStatus = QStatus.QUARANTINED


@dataclass(frozen=True)
class ShelfRule:
    """按用途生效的保质期规则。

    - purpose 为 None 表示兜底用途规则；
    - 同一 (purpose, version) 仅一条，按 effective_at 起生效；
    - 失效日 = 生产日期 + shelf_life_days 天，按 timezone 解释到当天 00:00；
    - at == 失效日 即视为过期（边界在演示中可独立核验）。
    """
    rule_id: str
    purpose: str | None
    version: str
    effective_at: datetime
    shelf_life_days: int
    timezone: str


@dataclass(frozen=True)
class Order:
    order_id: str
    purpose: str
    qty: int               # 整数需求量
    priority: int          # 数字越大优先级越高


@dataclass
class Dataset:
    name: str
    batches: dict[str, Batch]
    quantity_events: list[QuantityEvent]
    status_events: list[StatusEvent]
    rules: list[ShelfRule]
    orders: list[Order]
    input_hash: str = ""

    def with_orders(self, orders: list[Order]) -> "Dataset":
        return Dataset(self.name, self.batches, self.quantity_events,
                       self.status_events, self.rules, orders, self.input_hash)


# --------------------------------------------------------------------------- #
# 解析辅助
# --------------------------------------------------------------------------- #
def parse_dt(value: str, field_name: str) -> datetime:
    """解析 ISO-8601 时刻；强制要求带时区（业务时点必须是绝对时刻）。"""
    if not isinstance(value, str):
        raise InputError(f"{field_name} 必须是 ISO-8601 字符串，实际为 {type(value)!r}")
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InputError(f"{field_name} 时间格式无法解析: {value!r}") from exc
    if dt.tzinfo is None:
        raise InputError(f"{field_name} 必须带时区偏移/时区名: {value!r}")
    return dt


def parse_date(value: str, field_name: str) -> datetime:
    """解析生产日期（YYYY-MM-DD），解释为该日 00:00（附着 UTC，规则时区仅在算失效日时使用）。"""
    if not isinstance(value, str):
        raise InputError(f"{field_name} 必须是 YYYY-MM-DD 字符串")
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise InputError(f"{field_name} 日期格式无法解析: {value!r}") from exc
    return d.replace(tzinfo=timezone.utc)


def _require(obj: Mapping[str, Any], key: str, ctx: str) -> Any:
    if key not in obj:
        raise InputError(f"{ctx} 缺少必填字段 {key!r}")
    return obj[key]


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_dataset(raw: Mapping[str, Any]) -> Dataset:
    """把原始 JSON 结构解析为 Dataset，并做结构级校验。

    历史余额合法性（负库存等）在账本构建阶段检查，错误类型为 InvalidHistory。
    """
    name = str(raw.get("name", "dataset"))

    batches: dict[str, Batch] = {}
    for b in raw.get("batches", []):
        bid = _require(b, "batch_id", "batch")
        if bid in batches:
            raise InputError(f"批次 {bid} 重复定义")
        purposes = b.get("purposes")
        if not purposes or not isinstance(purposes, list) or not all(isinstance(p, str) for p in purposes):
            raise InputError(f"批次 {bid} 的 purposes 必须是非空字符串数组")
        init = QStatus(b.get("initial_status", QStatus.QUARANTINED.value))
        batches[bid] = Batch(
            batch_id=bid,
            production_date=parse_date(_require(b, "production_date", f"batch {bid}"),
                                       f"batch {bid}.production_date"),
            purposes=frozenset(purposes),
            initial_status=init,
        )

    q_events: list[QuantityEvent] = []
    seen_q: set[str] = set()
    for e in raw.get("quantity_events", []):
        eid = _require(e, "event_id", "quantity_event")
        if eid in seen_q:
            raise InputError(f"数量事件 id 重复: {eid}（事件身份必须唯一）")
        seen_q.add(eid)
        bid = _require(e, "batch_id", f"event {eid}")
        if bid not in batches:
            raise InvalidHistory(f"事件 {eid} 引用未知批次 {bid}")
        etype = QEventType(_require(e, "type", f"event {eid}"))
        qty = _require(e, "qty", f"event {eid}")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise InvalidHistory(
                f"事件 {eid} 数量非法: {qty!r}（必须为正整数；无效数量历史须拒绝，"
                "不得用负库存补方案）")
        sealed = e.get("sealed")
        accepted = e.get("accepted")
        if etype is QEventType.RETURN:
            if sealed is None or accepted is None:
                raise InputError(f"退回事件 {eid} 必须显式给出 sealed 与 accepted")
        q_events.append(QuantityEvent(
            event_id=eid, batch_id=bid, etype=etype, qty=qty,
            effective_at=parse_dt(_require(e, "effective_at", f"event {eid}"),
                                  f"event {eid}.effective_at"),
            sealed=sealed, accepted=accepted, note=str(e.get("note", "")),
        ))

    s_events: list[StatusEvent] = []
    seen_s: set[str] = set()
    for e in raw.get("status_events", []):
        eid = _require(e, "event_id", "status_event")
        if eid in seen_s:
            raise InputError(f"状态事件 id 重复: {eid}")
        seen_s.add(eid)
        bid = _require(e, "batch_id", f"event {eid}")
        if bid not in batches:
            raise InputError(f"状态事件 {eid} 引用未知批次 {bid}")
        status = QStatus(_require(e, "status", f"event {eid}"))
        evidence = e.get("evidence_id")
        # 放行允许暂无证据，但引擎会以 QUALITY_EVIDENCE_MISSING 阻断分配
        s_events.append(StatusEvent(
            event_id=eid, batch_id=bid, status=status,
            effective_at=parse_dt(_require(e, "effective_at", f"event {eid}"),
                                  f"event {eid}.effective_at"),
            evidence_id=evidence, note=str(e.get("note", "")),
        ))

    rules: list[ShelfRule] = []
    rule_keys: set[tuple[str | None, str, datetime]] = set()
    for r in raw.get("shelf_rules", []):
        rid = _require(r, "rule_id", "shelf_rule")
        purpose = r.get("purpose")  # None = 兜底规则
        version = str(_require(r, "version", f"rule {rid}"))
        eff = parse_dt(_require(r, "effective_at", f"rule {rid}"),
                       f"rule {rid}.effective_at")
        days = _require(r, "shelf_life_days", f"rule {rid}")
        if not isinstance(days, int) or isinstance(days, bool) or days < 0:
            raise InputError(f"规则 {rid} shelf_life_days 必须是非负整数")
        tzname = str(_require(r, "timezone", f"rule {rid}"))
        try:
            ZoneInfo(tzname)
        except Exception as exc:
            raise InputError(f"规则 {rid} 时区无法识别: {tzname!r}") from exc
        key = (purpose, version, eff)
        if key in rule_keys:
            raise InputError(f"规则重复 (purpose={purpose}, version={version}, effective_at={eff})")
        rule_keys.add(key)
        rules.append(ShelfRule(rid, purpose, version, eff, days, tzname))
    if not rules:
        raise InputError("至少需要一条保质期规则")

    orders: list[Order] = []
    oids: set[str] = set()
    for o in raw.get("orders", []):
        oid = _require(o, "order_id", "order")
        if oid in oids:
            raise InputError(f"订单 id 重复: {oid}")
        oids.add(oid)
        qty = _require(o, "qty", f"order {oid}")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise InputError(f"订单 {oid} 需求量必须为正整数")
        priority = _require(o, "priority", f"order {oid}")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise InputError(f"订单 {oid} 优先级必须为整数")
        purpose = str(_require(o, "purpose", f"order {oid}"))
        orders.append(Order(oid, purpose, qty, priority))

    input_hash = hashlib.sha256(_canonical_json(raw)).hexdigest()[:16]
    return Dataset(name=name, batches=batches, quantity_events=q_events,
                   status_events=s_events, rules=rules, orders=orders,
                   input_hash=input_hash)


def load_dataset(path: str) -> Dataset:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return parse_dataset(raw)


# 供 registry / 报告复用的序列化工具
def to_plain(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, frozenset):
        return sorted(obj)
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_plain(v) for k, v in asdict(obj).items()}
    return obj
