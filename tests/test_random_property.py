"""随机性质测试：随机生成小实例，分支定界目标必须与独立穷举完全一致。

运行：python -I tests/test_random_property.py
"""
from __future__ import annotations

import os
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from batch_alloc import (  # noqa: E402
    Batch, Dataset, Order, QStatus, QuantityEvent, StatusEvent, ShelfRule,
    Engine, solve, verify_plan, WarehouseService, parse_writeoff,
)
from batch_alloc.planner import brute_force_verify  # noqa: E402


def make_dataset(rng: random.Random, k: int) -> Dataset:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    purposes = ["A", "B", "C"]
    batches = {}
    q_events = []
    s_events = []
    rules = [ShelfRule("R1", None, "v1", datetime(2025, 1, 1, tzinfo=timezone.utc),
                       720, "UTC")]
    for i in range(k):
        bid = f"B{i}"
        n_p = rng.randint(1, 3)
        batches[bid] = Batch(
            bid, datetime(2026, 1, 1, tzinfo=timezone.utc),
            frozenset(rng.sample(purposes, n_p)), QStatus.QUARANTINED)
        qty = rng.randint(1, 50)
        q_events.append(QuantityEvent(
            f"E-R{i}", bid, "RECEIPT", qty,
            now - timedelta(days=100)))
        # 随机质量状态：约 80% 放行且带证据，其余隔离/召回
        roll = rng.random()
        if roll < 0.8:
            s_events.append(StatusEvent(
                f"E-S{i}", bid, QStatus.RELEASED, now - timedelta(days=90),
                evidence_id=f"EV-Q{i}"))
        elif roll < 0.9:
            s_events.append(StatusEvent(
                f"E-S{i}", bid, QStatus.QUARANTINED, now - timedelta(days=90)))
        else:
            s_events.append(StatusEvent(
                f"E-S{i}", bid, QStatus.RECALLED, now - timedelta(days=90)))

    orders = []
    for j in range(k):
        orders.append(Order(
            f"O{j}", rng.choice(purposes), rng.randint(1, 40),
            rng.choice([1, 5, 10])))

    return Dataset("rand", batches, q_events, s_events, rules, orders, "rand")


class RandomProperty(unittest.TestCase):
    def test_100_random_instances(self):
        rng = random.Random(20261125)
        for trial in range(100):
            k = rng.randint(2, 6)
            ds = make_dataset(rng, k)
            at = datetime(2026, 6, 1, tzinfo=timezone.utc)
            engine = Engine(ds, at, "v1")
            result = solve(engine, f"T{trial}", "v1", "rand")
            bf_obj, bf_assigns = brute_force_verify(engine)
            self.assertEqual(
                tuple(result.objective), tuple(bf_obj),
                msg=f"trial {trial}: {result.objective} != {bf_obj}")
            got = {a.order_id: a.batch_id for a in result.allocations}
            self.assertIn(got, bf_assigns, msg=f"trial {trial} 分配不在最优解集")
            v = verify_plan(ds, result, at, "v1", brute_force=False)
            self.assertTrue(v["ok"], msg=f"trial {trial}: {v['errors']}")
            # 直接核验占用不超余量
            for bid, used in v["batch_usage"].items():
                self.assertLessEqual(used, v["balances"][bid])


class WriteOffRandomProperty(unittest.TestCase):
    """随机核销 + 重算性质：不超额、恰好一次、库存恒非负、剩余量自洽。"""

    def test_random_writeoffs_and_recalcs(self):
        import tempfile
        from batch_alloc import build_ledger
        rng = random.Random(20261125)
        at = datetime(2026, 6, 1, tzinfo=timezone.utc)
        now = datetime(2026, 6, 2, tzinfo=timezone.utc)
        for trial in range(40):
            with tempfile.TemporaryDirectory() as tmp:
                import os
                svc = WarehouseService(os.path.join(tmp, "reg.json"),
                                       os.path.join(tmp, "wo.json"))
                k = rng.randint(2, 5)
                ds = make_dataset(rng, k)
                plan = svc.recalc(ds, at, f"P{trial}", "v1", now)
                allocs = list(plan.allocations)
                if not allocs:
                    continue
                shipped: dict[str, int] = {}
                seq = 0
                for _ in range(12):
                    a = rng.choice(allocs)
                    seq += 1
                    remaining = a.qty - shipped.get(a.order_id, 0)
                    # 一半概率出合法量，一半概率尝试超量/错批（必须被拒绝）
                    if rng.random() < 0.7 and remaining > 0:
                        qty = rng.randint(1, max(1, remaining + 2))
                        bid = a.batch_id
                    else:
                        qty = rng.randint(1, 5)
                        others = [b for b in ds.batches if b != a.batch_id]
                        bid = rng.choice(others) if others else a.batch_id
                    req = parse_writeoff({
                        "writeoff_id": f"WO{trial}-{seq}",
                        "plan_id": f"P{trial}",
                        "outbound_at": (at + timedelta(days=seq)).isoformat(),
                        "lines": [{"order_id": a.order_id, "batch_id": bid,
                                   "qty": qty}],
                    })
                    res = svc.submit_writeoff(ds, req, now)
                    if res.accepted:
                        shipped[a.order_id] = shipped.get(a.order_id, 0) + qty
                        # 接受 => 批次必须与方案一致且累计不超分配
                        self.assertEqual(bid, a.batch_id)
                        self.assertLessEqual(shipped[a.order_id], a.qty)
                    else:
                        # 拒绝 => 该单事实不得入账，出库量不变
                        pass

                # 性质1：订单累计出库绝不超过原分配量
                for a in allocs:
                    self.assertLessEqual(shipped.get(a.order_id, 0), a.qty)

                # 性质2：重算后合成事件每个单号每批仅一条（恰好一次），库存非负
                p2 = svc.recalc(ds, at, f"Q{trial}", "v1", now)
                aug = svc.effective_dataset(ds, at)
                synth_ids = [e.event_id for e in aug.quantity_events
                             if e.event_id.startswith("WRITEOFF:")]
                self.assertEqual(len(synth_ids), len(set(synth_ids)))
                ledger = build_ledger(aug, at)
                self.assertTrue(all(v >= 0 for v in ledger.balances.values()))

                # 性质3：重算方案的批次占用不超过含出库事实的实际库存
                v = verify_plan(aug, p2, at, "v1", brute_force=False)
                self.assertTrue(v["ok"], msg=f"trial {trial}: {v['errors']}")

                # 性质4：订单执行视图自洽
                for row in svc.order_execution(ds):
                    self.assertEqual(
                        row.remaining_qty,
                        max(0, row.allocated_qty - row.shipped_qty))
                    self.assertEqual(row.shipped_qty, shipped.get(row.order_id, 0))

                # 性质5：幂等重送不改变任何事实
                accepted_recs = svc.list_writeoffs("ACCEPTED")
                if accepted_recs:
                    rec = rng.choice(accepted_recs)
                    req2 = parse_writeoff({
                        "writeoff_id": rec.writeoff_id,
                        "plan_id": rec.plan_id,
                        "outbound_at": rec.outbound_at.isoformat(),
                        "lines": [{"order_id": ln.order_id, "batch_id": ln.batch_id,
                                   "qty": ln.qty} for ln in rec.lines],
                    })
                    before = len(svc.list_writeoffs())
                    res = svc.submit_writeoff(ds, req2, now)
                    self.assertTrue(res.accepted and res.duplicate)
                    self.assertEqual(len(svc.list_writeoffs()), before)
                    stock_before = {s.batch_id: s.on_hand
                                    for s in svc.stock_view(ds)}
                    svc.submit_writeoff(ds, req2, now)
                    stock_after = {s.batch_id: s.on_hand
                                   for s in svc.stock_view(ds)}
                    self.assertEqual(stock_before, stock_after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
