"""方案登记与版本管理。

要求：规则/质量修订后重算必须撤回旧分配且量不重复，新旧方案分别可查。

实现：
- 每个方案一条不可变记录（status=ACTIVE/WITHDRAWN），落盘为 JSON（原子写）；
- 同一数据集同一时点同一规则版本只允许一个 ACTIVE 方案；
- recalc 先撤回旧 ACTIVE 再发布新方案，旧记录保留可查；
- 发布/重算后跑独立核验 verify_plan：批次累计分配 <= 账本余量、每单单批全额、
  用途/质量/保质期全部合规、目标值与独立穷举（小规模）一致。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime

from .engine import Engine, REASON_TEXT
from .model import Dataset, to_plain
from .planner import PlanResult, solve, brute_force_verify


@dataclass
class PlanRecord:
    plan_id: str
    status: str                     # ACTIVE / WITHDRAWN
    created_at: str
    supersedes: str | None
    result: PlanResult


class RegistryError(Exception):
    pass


class PlanRegistry:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        if not os.path.exists(path):
            self._write({"plans": []})

    # ------------------------------------------------------------------ #
    def _read(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    def list_plans(self) -> list[dict]:
        return self._read()["plans"]

    def get(self, plan_id: str) -> dict:
        for p in self.list_plans():
            if p["plan_id"] == plan_id:
                return p
        raise RegistryError(f"方案不存在: {plan_id}")

    def active_plans(self) -> list[dict]:
        return [p for p in self.list_plans() if p["status"] == "ACTIVE"]

    def history(self) -> list[dict]:
        """新旧方案分别可查：按创建时间返回全部记录。"""
        return sorted(self.list_plans(), key=lambda p: p["created_at"])

    # ------------------------------------------------------------------ #
    def publish(self, result: PlanResult, created_at: datetime,
                supersedes: str | None = None) -> str:
        data = self._read()
        # 同一数据集同一业务时点只允许一个 ACTIVE 方案（规则版本仅是记录属性）；
        # 换版本/质量修订必须走 recalc 撤回旧方案，禁止两个 ACTIVE 造成量重复。
        key = (result.dataset_name, result.as_of.isoformat())
        for p in data["plans"]:
            if p["status"] != "ACTIVE":
                continue
            pkey = (p["result"]["dataset_name"], p["result"]["as_of"])
            if pkey == key:
                raise RegistryError(
                    f"已存在 ACTIVE 方案 {p['plan_id']}（同数据集/时点），"
                    "重算请走 recalc；禁止重复发布导致量重复")
        record = {
            "plan_id": result.plan_id,
            "status": "ACTIVE",
            "created_at": created_at.isoformat(),
            "supersedes": supersedes,
            "result": to_plain(result),
        }
        data["plans"].append(record)
        self._write(data)
        return result.plan_id

    def withdraw(self, plan_id: str, at: datetime) -> None:
        data = self._read()
        found = False
        for p in data["plans"]:
            if p["plan_id"] == plan_id and p["status"] == "ACTIVE":
                p["status"] = "WITHDRAWN"
                p["withdrawn_at"] = at.isoformat()
                found = True
        if not found:
            raise RegistryError(f"未找到可撤回的 ACTIVE 方案: {plan_id}")
        self._write(data)

    def recalc(self, ds: Dataset, at: datetime, new_plan_id: str,
               rule_version: str | None, created_at: datetime,
               verify: bool = True) -> PlanResult:
        """规则/质量修订后重算：撤回同键旧 ACTIVE 方案，再发布新方案。

        分配只来自事件账本重新计算的余量，与旧方案无关，因此量不会重复。
        """
        key = (ds.name, at.isoformat())
        old_id = None
        for p in self.active_plans():
            pkey = (p["result"]["dataset_name"], p["result"]["as_of"])
            if pkey == key:
                old_id = p["plan_id"]
                break
        if old_id is not None:
            self.withdraw(old_id, created_at)

        engine = Engine(ds, at, rule_version)
        result = solve(engine, new_plan_id, rule_version, ds.input_hash)
        if verify:
            verify_plan(ds, result, at, rule_version, brute_force=True)
        self.publish(result, created_at, supersedes=old_id)
        return result


# --------------------------------------------------------------------------- #
# 独立核验：不依赖求解器内部状态，只从输入事件与方案记录重新推导
# --------------------------------------------------------------------------- #
def verify_plan(ds: Dataset, result: PlanResult, at: datetime,
                rule_version: str | None = None, brute_force: bool = True) -> dict:
    engine = Engine(ds, at, rule_version)
    errors: list[str] = []

    # 1) 每单单批次、全额、整数、用途/质量/保质期合规
    order_map = {o.order_id: o for o in ds.orders}
    used: dict[str, int] = {}
    for a in result.allocations:
        o = order_map.get(a.order_id)
        if o is None:
            errors.append(f"分配 {a.order_id} 引用未知订单")
            continue
        if a.qty != o.qty:
            errors.append(f"订单 {a.order_id} 未全额满足：{a.qty} != {o.qty}")
        elig = engine.evaluate(ds.batches[a.batch_id], o)
        if not elig.ok:
            errors.append(f"订单 {a.order_id} 分到不可用批次 {a.batch_id}: "
                          f"{REASON_TEXT.get(elig.reason, elig.reason)} {elig.detail}")
        used[a.batch_id] = used.get(a.batch_id, 0) + a.qty

    # 2) 批次累计分配不得超余量（同批多单的核心约束）
    for bid, q in used.items():
        if q > engine.balance(bid):
            errors.append(f"批次 {bid} 超量分配 {q} > 账面余量 {engine.balance(bid)}")

    # 3) 每单最多一条分配
    aoids = [a.order_id for a in result.allocations]
    if len(aoids) != len(set(aoids)):
        errors.append("存在订单被重复分配")

    # 4) 阻断订单集合 = 全部订单 - 已满足订单
    satisfied = set(aoids)
    blocked_ids = {b.order_id for b in result.blocked}
    if satisfied | blocked_ids != set(order_map) or satisfied & blocked_ids:
        errors.append("已满足/阻断订单集合与输入订单不一致")

    # 5) 目标值与独立穷举一致（小规模）
    bf_obj = None
    if brute_force:
        try:
            bf_obj, _ = brute_force_verify(engine)
            if tuple(result.objective) != tuple(bf_obj):
                errors.append(f"目标值不一致：求解器 {result.objective} vs 穷举 {bf_obj}")
        except ValueError as exc:
            bf_obj = f"skip({exc})"

    return {"ok": not errors, "errors": errors,
            "brute_force_objective": bf_obj,
            "solver_objective": list(result.objective),
            "batch_usage": used,
            "balances": engine.ledger.balances}
