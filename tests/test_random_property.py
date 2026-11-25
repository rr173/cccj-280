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
    Engine, solve, verify_plan,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
