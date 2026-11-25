"""单元/性质测试：python -I tests/test_batch_alloc.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from batch_alloc import (  # noqa: E402
    parse_dataset, parse_dt, Engine, solve, verify_plan, PlanRegistry,
    InvalidHistory, InputError, RegistryError, to_plain,
)
from batch_alloc.planner import brute_force_verify  # noqa: E402
from batch_alloc.engine import expiry_datetime, select_rule, build_ledger  # noqa: E402

IN = os.path.join(ROOT, "sample_inputs")


def load(name):
    with open(os.path.join(IN, name), encoding="utf-8") as fh:
        return parse_dataset(json.load(fh))


def raw(name):
    with open(os.path.join(IN, name), encoding="utf-8") as fh:
        return json.load(fh)


class TestLedger(unittest.TestCase):
    def test_dedup_by_event_identity(self):
        ds = load("demo_returns.json")
        # 追加一条同身份事件（不同数量），解析阶段即按重复 id 拒绝
        data = raw("demo_returns.json")
        dup = dict(data["quantity_events"][0])
        dup["qty"] = 999
        data["quantity_events"].append(dup)
        with self.assertRaises(InputError):
            parse_dataset(data)
        # 原数据集账本正常
        eng = Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))
        self.assertEqual(eng.balance("B-R1"), 80)

    def test_rejected_returns_not_counted(self):
        ds = load("demo_returns.json")
        eng = Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))
        ids = {e.event_id: e for e in eng.ledger.entries}
        self.assertTrue(ids["EV-205"].counted)
        self.assertFalse(ids["EV-206"].counted)
        self.assertFalse(ids["EV-207"].counted)
        self.assertEqual(len(eng.ledger.rejected_returns), 2)
        self.assertEqual(eng.balance("B-R1"), 80)  # 100-30+10，两笔退回被拒

    def test_negative_quantity_rejected(self):
        with self.assertRaises(InvalidHistory):
            ds = load("demo_invalid_qty.json")
            Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))

    def test_negative_balance_rejected(self):
        with self.assertRaises(InvalidHistory):
            ds = load("demo_negative_stock.json")
            Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))

    def test_scrap_reduces_balance(self):
        data = raw("demo_returns.json")
        data["name"] = "scrap"
        data["quantity_events"].append(
            {"event_id": "EV-SC", "batch_id": "B-R2", "type": "SCRAP", "qty": 10,
             "effective_at": "2026-04-01T00:00:00Z"})
        ds = parse_dataset(data)
        eng = Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))
        self.assertEqual(eng.balance("B-R2"), 40)

    def test_as_of_filters_future_events(self):
        ds = load("demo_returns.json")
        eng = Engine(ds, parse_dt("2026-02-15T00:00:00Z", "at"))
        self.assertEqual(eng.balance("B-R1"), 70)   # 入库100-领用30，退回未发生
        self.assertEqual(eng.balance("B-R2"), 50)


class TestQuality(unittest.TestCase):
    def setUp(self):
        self.ds = load("demo_quality.json")
        self.at = parse_dt("2026-06-01T00:00:00Z", "at")
        self.eng = Engine(self.ds, self.at)

    def test_status_never_changes_balance(self):
        # B-Q2 经历 放行->隔离->放行，账面始终等于入库 100
        self.assertEqual(self.eng.balance("B-Q2"), 100)
        early = Engine(self.ds, parse_dt("2026-02-01T00:00:00Z", "at"))
        self.assertEqual(early.balance("B-Q2"), 100)

    def test_each_gate_blocks(self):
        want = {"O-REC": ("B-REC", "RECALLED"),
                "O-Q": ("B-Q", "QUARANTINED"),
                "O-NE": ("B-NE", "QUALITY_EVIDENCE_MISSING"),
                "O-EXP": ("B-EXP", "EXPIRED")}
        for oid, (bid, reason) in want.items():
            order = next(o for o in self.ds.orders if o.order_id == oid)
            elig = self.eng.evaluate(self.ds.batches[bid], order)
            self.assertFalse(elig.ok)
            self.assertEqual(elig.reason, reason)

    def test_released_after_quarantine_usable(self):
        order = next(o for o in self.ds.orders if o.order_id == "O-Q2")
        elig = self.eng.evaluate(self.ds.batches["B-Q2"], order)
        self.assertTrue(elig.ok)


class TestShelfLife(unittest.TestCase):
    def setUp(self):
        self.ds = load("demo_shelf.json")

    def test_timezone_expiry_instants(self):
        batch_f = self.ds.batches["B-SH"]
        batch_g = self.ds.batches["B-SH2"]
        r_f = select_rule(self.ds.rules, "F", parse_dt("2026-03-10T00:00:00Z", "t"), "v1")
        r_g = select_rule(self.ds.rules, "G", parse_dt("2026-03-10T00:00:00Z", "t"), "v1")
        exp_f = expiry_datetime(batch_f, r_f)
        exp_g = expiry_datetime(batch_g, r_g)
        # 同一日历时日，不同时区 => 不同绝对时刻
        self.assertEqual(exp_f.astimezone(timezone.utc).isoformat(),
                         "2026-03-30T16:00:00+00:00")
        self.assertEqual(exp_g.astimezone(timezone.utc).isoformat(),
                         "2026-03-31T00:00:00+00:00")

    def test_boundary_expiry_day_is_expired(self):
        order = next(o for o in self.ds.orders if o.order_id == "O-F")
        before = Engine(self.ds, parse_dt("2026-03-30T15:59:59Z", "t"), "v1")
        on_day = Engine(self.ds, parse_dt("2026-03-30T16:00:00Z", "t"), "v1")
        self.assertTrue(before.evaluate(self.ds.batches["B-SH"], order).ok)
        self.assertFalse(on_day.evaluate(self.ds.batches["B-SH"], order).ok)

    def test_rule_version_changes_expiry(self):
        order = next(o for o in self.ds.orders if o.order_id == "O-F")
        at = parse_dt("2026-03-20T00:00:00Z", "t")
        self.assertTrue(Engine(self.ds, at, "v1")
                        .evaluate(self.ds.batches["B-SH"], order).ok)
        self.assertFalse(Engine(self.ds, at, "v2")
                         .evaluate(self.ds.batches["B-SH"], order).ok)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(InputError):
            parse_dt("2026-03-20T00:00:00", "t")


class TestSolver(unittest.TestCase):
    def test_matches_bruteforce_all_samples(self):
        cases = [
            ("demo_wide_narrow.json", "2026-06-01T00:00:00Z", None),
            ("demo_quality.json", "2026-06-01T00:00:00Z", None),
            ("demo_returns.json", "2026-06-01T00:00:00Z", None),
            ("demo_shelf.json", "2026-03-20T00:00:00Z", "v1"),
            ("demo_shelf.json", "2026-03-20T00:00:00Z", "v2"),
            ("demo_priority.json", "2026-06-01T00:00:00Z", None),
        ]
        for fname, at_s, ver in cases:
            with self.subTest(file=fname, ver=ver):
                ds = load(fname)
                eng = Engine(ds, parse_dt(at_s, "at"), ver)
                result = solve(eng, "X", ver, ds.input_hash)
                bf_obj, bf_assigns = brute_force_verify(eng)
                self.assertEqual(tuple(result.objective), tuple(bf_obj))
                got = {a.order_id: a.batch_id for a in result.allocations}
                self.assertIn(got, bf_assigns)
                v = verify_plan(ds, result, parse_dt(at_s, "at"), ver)
                self.assertTrue(v["ok"], v["errors"])

    def test_no_split_or_partial(self):
        ds = load("demo_returns.json")
        eng = Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))
        result = solve(eng, "X", input_hash=ds.input_hash)
        for a in result.allocations:
            order = next(o for o in ds.orders if o.order_id == a.order_id)
            self.assertEqual(a.qty, order.qty)  # 全额
        # O-SHORT 需求 51，最大单批余量 50 => 不可部分满足
        self.assertEqual({b.order_id for b in result.blocked}, {"O-SHORT"})

    def test_batch_cumulative_usage_within_balance(self):
        ds = load("demo_quality.json")
        eng = Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))
        result = solve(eng, "X", input_hash=ds.input_hash)
        usage = {}
        for a in result.allocations:
            usage[a.batch_id] = usage.get(a.batch_id, 0) + a.qty
        for bid, q in usage.items():
            self.assertLessEqual(q, eng.balance(bid))

    def test_priority_lexicographic(self):
        ds = load("demo_priority.json")
        eng = Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))
        result = solve(eng, "X", input_hash=ds.input_hash)
        self.assertEqual([a.order_id for a in result.allocations], ["O-HI"])
        self.assertEqual(tuple(result.objective), (1, 0, 100))

    def test_bruteforce_detects_tampered_plan(self):
        ds = load("demo_wide_narrow.json")
        at = parse_dt("2026-06-01T00:00:00Z", "at")
        eng = Engine(ds, at)
        result = solve(eng, "X", input_hash=ds.input_hash)
        # 人为制造超量分配（同批重复占用），核验必须失败
        result.allocations[0].__dict__["batch_id"] = "B-WIDE"
        v = verify_plan(ds, result, at, brute_force=False)
        self.assertFalse(v["ok"])
        self.assertTrue(any("超量" in e for e in v["errors"]))


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg_path = os.path.join(self.tmp.name, "registry.json")
        self.reg = PlanRegistry(self.reg_path)
        self.ds = load("demo_shelf.json")
        self.at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.now = parse_dt("2026-03-20T01:00:00Z", "now")

    def tearDown(self):
        self.tmp.cleanup()

    def test_recalc_withdraws_and_keeps_history(self):
        p1 = self.reg.recalc(self.ds, self.at, "P1", "v1", self.now)
        p2 = self.reg.recalc(self.ds, self.at, "P2", "v2", self.now)
        self.assertEqual(len(self.reg.active_plans()), 1)
        self.assertEqual(self.reg.active_plans()[0]["plan_id"], "P2")
        ids = [(p["plan_id"], p["status"]) for p in self.reg.history()]
        self.assertEqual(ids, [("P1", "WITHDRAWN"), ("P2", "ACTIVE")])
        self.assertEqual(self.reg.get("P1")["status"], "WITHDRAWN")  # 旧方案可查
        self.assertEqual(p2.rule_version, "v2")

    def test_duplicate_active_publish_rejected(self):
        self.reg.recalc(self.ds, self.at, "P1", "v1", self.now)
        eng = Engine(self.ds, self.at, "v1")
        again = solve(eng, "P1B", "v1", self.ds.input_hash)
        with self.assertRaises(RegistryError):
            self.reg.publish(again, self.now)

    def test_recalc_after_recall_drops_allocation(self):
        self.reg.recalc(self.ds, self.at, "P1", "v1", self.now)
        data = raw("demo_shelf.json")
        data["status_events"].append({
            "event_id": "EV-REC", "batch_id": "B-SH2", "status": "RECALLED",
            "effective_at": "2026-03-19T00:00:00Z"})
        ds2 = parse_dataset(data)
        p2 = self.reg.recalc(ds2, self.at, "P2", "v1", self.now)
        # B-SH2 被召回 => O-G 失去唯一批次；O-F 仍由 B-SH 满足
        self.assertEqual([a.order_id for a in p2.allocations], ["O-F"])
        self.assertEqual({b.order_id for b in p2.blocked}, {"O-G"})
        # 旧 ACTIVE 已撤回，不存在量重复
        active = self.reg.active_plans()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["plan_id"], "P2")

    def test_persistence_roundtrip(self):
        p1 = self.reg.recalc(self.ds, self.at, "P1", "v1", self.now)
        reg2 = PlanRegistry(self.reg_path)  # 重新打开
        self.assertEqual(reg2.get("P1")["result"]["plan_id"], p1.plan_id)
        self.assertEqual(reg2.get("P1")["result"]["objective"], list(p1.objective))


class TestSerialization(unittest.TestCase):
    def test_to_plain_json_safe(self):
        ds = load("demo_returns.json")
        eng = Engine(ds, parse_dt("2026-06-01T00:00:00Z", "at"))
        result = solve(eng, "P", input_hash=ds.input_hash)
        s = json.dumps(to_plain(result), ensure_ascii=False)
        back = json.loads(s)
        self.assertEqual(back["plan_id"], "P")
        self.assertIn("allocations", back)


if __name__ == "__main__":
    unittest.main(verbosity=2)
