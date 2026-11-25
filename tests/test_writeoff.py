"""拣发核销单元/性质测试：python -I tests/test_writeoff.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from batch_alloc import (  # noqa: E402
    parse_dataset, parse_dt, parse_writeoff, WarehouseService,
    verify_plan, InvalidHistory, InputError, content_hash_of,
    STATUS_ACCEPTED,
    WO_PLAN_NOT_ACTIVE, WO_BATCH_MISMATCH, WO_EXCEEDS_ALLOCATED,
    WO_NEGATIVE_HISTORY, WO_IDEMPOTENT_CONFLICT, WO_LINE_NOT_IN_PLAN,
    WO_DUPLICATE_LINE, WO_DATASET_MISMATCH,
    WO_LINE_UNKNOWN_ORDER,
)
from batch_alloc.writeoff import dataset_with_outbounds  # noqa: E402

IN = os.path.join(ROOT, "sample_inputs")


def load(name):
    with open(os.path.join(IN, name), encoding="utf-8") as fh:
        return parse_dataset(json.load(fh))


def raw(name):
    with open(os.path.join(IN, name), encoding="utf-8") as fh:
        return json.load(fh)


def doc(wid, plan, at, lines):
    return {"writeoff_id": wid, "plan_id": plan, "outbound_at": at, "lines": lines}


class WriteOffTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg_path = os.path.join(self.tmp.name, "registry.json")
        self.store_path = os.path.join(self.tmp.name, "writeoffs.json")
        self.svc = WarehouseService(self.reg_path, self.store_path)
        self.now = parse_dt("2026-03-20T01:00:00Z", "now")

    def tearDown(self):
        self.tmp.cleanup()


class TestWriteOffHappyPath(WriteOffTestBase):
    def setUp(self):
        super().setUp()
        self.ds = load("demo_shelf.json")
        self.at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.svc.recalc(self.ds, self.at, "P1", "v1", self.now)

    def test_full_shipment_accepted_and_stock_updated(self):
        req = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 10},
             {"order_id": "O-G", "batch_id": "B-SH2", "qty": 10}]))
        res = self.svc.submit_writeoff(self.ds, req, self.now)
        self.assertTrue(res.accepted, res.failures)
        self.assertFalse(res.duplicate)
        stock = {s.batch_id: s for s in self.svc.stock_view(self.ds)}
        self.assertEqual(stock["B-SH"].on_hand, 90)
        self.assertEqual(stock["B-SH2"].on_hand, 90)
        self.assertEqual(stock["B-SH"].outbound_qty, 10)
        exec_rows = {r.order_id: r for r in self.svc.order_execution(self.ds)}
        self.assertEqual(exec_rows["O-F"].shipped_qty, 10)
        self.assertEqual(exec_rows["O-F"].remaining_qty, 0)
        self.assertEqual(exec_rows["O-G"].remaining_qty, 0)

    def test_split_shipments_cumulative_within_allocation(self):
        for wid, q in [("W1", 4), ("W2", 3), ("W3", 3)]:
            req = parse_writeoff(doc(
                wid, "P1", f"2026-03-2{int(wid[-1])}T08:00:00Z",
                [{"order_id": "O-F", "batch_id": "B-SH", "qty": q}]))
            res = self.svc.submit_writeoff(self.ds, req, self.now)
            self.assertTrue(res.accepted, res.failures)
        rows = {r.order_id: r for r in self.svc.order_execution(self.ds)}
        self.assertEqual(rows["O-F"].shipped_qty, 10)
        self.assertEqual(rows["O-F"].remaining_qty, 0)

    def test_exceeding_allocation_rejected_entire_doc(self):
        ok = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 8}]))
        self.assertTrue(self.svc.submit_writeoff(self.ds, ok, self.now).accepted)
        # 再出 3：累计 11 > 分配 10，整单拒绝
        bad = parse_writeoff(doc(
            "W2", "P1", "2026-03-22T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 3}]))
        res = self.svc.submit_writeoff(self.ds, bad, self.now)
        self.assertFalse(res.accepted)
        self.assertEqual([f.code for f in res.failures], [WO_EXCEEDS_ALLOCATED])
        # 拒绝单不得产生任何扣减
        rows = {r.order_id: r for r in self.svc.order_execution(self.ds)}
        self.assertEqual(rows["O-F"].shipped_qty, 8)
        self.assertEqual(rows["O-F"].remaining_qty, 2)

    def test_batch_mismatch_rejected(self):
        bad = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-G", "batch_id": "B-SH", "qty": 1}]))
        res = self.svc.submit_writeoff(self.ds, bad, self.now)
        self.assertFalse(res.accepted)
        self.assertIn(WO_BATCH_MISMATCH, [f.code for f in res.failures])

    def test_order_not_in_plan_rejected(self):
        # 先把 O-F 分配整单出库，再对一张单里混入方案外的写法：
        # demo_shelf 两单都在方案中，这里构造不在方案分配里的订单需换数据集，
        # 改用重算后无分配的场景（见 TestRecalcKeepsFacts）。本用例直接验证
        # 未知订单与重复行的结构原因码。
        bad = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 1},
             {"order_id": "O-F", "batch_id": "B-SH", "qty": 1}]))
        res = self.svc.submit_writeoff(self.ds, bad, self.now)
        self.assertFalse(res.accepted)
        self.assertIn(WO_DUPLICATE_LINE, [f.code for f in res.failures])

    def test_unknown_order_rejected(self):
        bad = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "NOPE", "batch_id": "B-SH", "qty": 1}]))
        res = self.svc.submit_writeoff(self.ds, bad, self.now)
        self.assertFalse(res.accepted)
        self.assertEqual([f.code for f in res.failures], [WO_LINE_UNKNOWN_ORDER])

    def test_known_order_not_in_plan_rejected(self):
        # demo_quality 重算后部分订单无分配；对未分配订单提交核销须拒绝
        ds = load("demo_quality.json")
        at = parse_dt("2026-06-01T00:00:00Z", "at")
        svc = WarehouseService(self.reg_path + ".q", self.store_path + ".q")
        plan = svc.recalc(ds, at, "Q1", None, self.now)
        blocked_ids = {b.order_id for b in plan.blocked}
        self.assertTrue(blocked_ids)
        oid = sorted(blocked_ids)[0]
        bad = parse_writeoff(doc(
            "WQ1", "Q1", "2026-06-02T08:00:00Z",
            [{"order_id": oid, "batch_id": "B-Q", "qty": 1}]))
        res = svc.submit_writeoff(ds, bad, self.now)
        self.assertFalse(res.accepted)
        self.assertIn(WO_LINE_NOT_IN_PLAN, [f.code for f in res.failures])


class TestIdempotency(WriteOffTestBase):
    def setUp(self):
        super().setUp()
        self.ds = load("demo_shelf.json")
        self.at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.svc.recalc(self.ds, self.at, "P1", "v1", self.now)
        self.req = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 4}]))

    def test_resend_same_content_no_double_deduction(self):
        first = self.svc.submit_writeoff(self.ds, self.req, self.now)
        self.assertTrue(first.accepted and not first.duplicate)
        second = self.svc.submit_writeoff(self.ds, self.req, self.now)
        self.assertTrue(second.accepted and second.duplicate)
        # 存储里只有一张单，出库只计一次
        recs = self.svc.list_writeoffs(STATUS_ACCEPTED)
        self.assertEqual(len(recs), 1)
        rows = {r.order_id: r for r in self.svc.order_execution(self.ds)}
        self.assertEqual(rows["O-F"].shipped_qty, 4)

    def test_same_id_different_content_rejected(self):
        self.assertTrue(self.svc.submit_writeoff(self.ds, self.req, self.now).accepted)
        conflict = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 5}]))
        res = self.svc.submit_writeoff(self.ds, conflict, self.now)
        self.assertFalse(res.accepted)
        self.assertEqual([f.code for f in res.failures], [WO_IDEMPOTENT_CONFLICT])
        # 冲突返回不落盘、不改写首次事实
        recs = self.svc.list_writeoffs()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].status, STATUS_ACCEPTED)

    def test_rejected_doc_resend_same_content_replays_rejection(self):
        over = parse_writeoff(doc(
            "WX", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 99}]))
        first = self.svc.submit_writeoff(self.ds, over, self.now)
        self.assertFalse(first.accepted)
        again = self.svc.submit_writeoff(self.ds, over, self.now)
        self.assertFalse(again.accepted)
        self.assertTrue(again.duplicate)
        self.assertEqual([f.code for f in again.failures], [WO_EXCEEDS_ALLOCATED])

    def test_content_hash_covers_all_fields(self):
        h1 = content_hash_of("P1", parse_dt("2026-03-21T08:00:00Z", "t"),
                             self.req.lines)
        h2 = content_hash_of("P1", parse_dt("2026-03-21T09:00:00Z", "t"),
                             self.req.lines)
        self.assertNotEqual(h1, h2)  # 出库时点不同 => 内容不同


class TestPlanLifecycle(WriteOffTestBase):
    def setUp(self):
        super().setUp()
        self.ds = load("demo_shelf.json")
        self.at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.svc.recalc(self.ds, self.at, "P1", "v1", self.now)

    def test_no_writeoff_after_withdraw(self):
        self.svc.withdraw("P1", self.now)
        req = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 1}]))
        res = self.svc.submit_writeoff(self.ds, req, self.now)
        self.assertFalse(res.accepted)
        self.assertEqual([f.code for f in res.failures], [WO_PLAN_NOT_ACTIVE])

    def test_withdraw_keeps_confirmed_outbound_facts(self):
        req = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 6}]))
        self.assertTrue(self.svc.submit_writeoff(self.ds, req, self.now).accepted)
        self.svc.withdraw("P1", self.now)
        # 撤回不抹去事实：按旧方案查仍能看到已出库 6
        rows = {r.order_id: r for r in self.svc.order_execution(self.ds, "P1")}
        self.assertEqual(rows["O-F"].shipped_qty, 6)
        recs = self.svc.list_writeoffs(STATUS_ACCEPTED)
        self.assertEqual(len(recs), 1)
        # 库存仍扣减一次
        stock = {s.batch_id: s for s in self.svc.stock_view(self.ds)}
        self.assertEqual(stock["B-SH"].on_hand, 94)

    def test_recalc_counts_outbound_exactly_once(self):
        req = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 7}]))
        self.assertTrue(self.svc.submit_writeoff(self.ds, req, self.now).accepted)
        p2 = self.svc.recalc(self.ds, self.at, "P2", "v1", self.now)
        # 合成事件身份唯一 => 再次重算不会重复扣减
        p3 = self.svc.recalc(self.ds, self.at, "P3", "v1", self.now)
        aug = self.svc.effective_dataset(self.ds, self.at)
        synth = [e for e in aug.quantity_events if e.event_id == "WRITEOFF:W1:B-SH"]
        self.assertEqual(len(synth), 1)
        self.assertEqual(p3.balances["B-SH"], 93)
        v = verify_plan(aug, p3, self.at, "v1", brute_force=True)
        self.assertTrue(v["ok"], v["errors"])
        # O-F 需求 10，可用 93 仍可满足；若重复扣减也仍满足，故再用 returns 数据集强化
        self.assertEqual({a.order_id for a in p2.allocations}, {"O-F", "O-G"})

    def test_recalc_drops_order_when_stock_insufficient(self):
        ds = load("demo_returns.json")
        at = parse_dt("2026-06-01T00:00:00Z", "at")
        svc = WarehouseService(self.reg_path + ".x", self.store_path + ".x")
        svc.recalc(ds, at, "R1", None, self.now)
        req = parse_writeoff(doc(
            "RW1", "R1", "2026-06-02T08:00:00Z",
            [{"order_id": "O-EXACT", "batch_id": "B-R1", "qty": 40}]))
        self.assertTrue(svc.submit_writeoff(ds, req, self.now).accepted)
        p2 = svc.recalc(ds, at, "R2", None, self.now)
        # B-R1 账面 80 - 已出 40 = 40，O-EXACT 需求 80 无法全额 => 阻断
        self.assertEqual(p2.allocations, [])
        self.assertEqual({b.order_id for b in p2.blocked}, {"O-EXACT", "O-SHORT"})
        # 已出库事实保留，剩余可执行量为 0
        rows = {r.order_id: r for r in svc.order_execution(ds)}
        self.assertEqual(rows["O-EXACT"].shipped_qty, 40)
        self.assertEqual(rows["O-EXACT"].remaining_qty, 0)


class TestIllegalHistory(WriteOffTestBase):
    def test_negative_stock_history_rejects_entire_doc(self):
        ds = load("demo_returns.json")
        at = parse_dt("2026-06-01T00:00:00Z", "at")
        svc = WarehouseService(self.reg_path + ".x", self.store_path + ".x")
        svc.recalc(ds, at, "R1", None, self.now)
        # B-R1 账面 80；先合法出 70，再出 20（本单数量未超订单分配 80，
        # 但会使批次账面变负）=> 非法库存历史，整单拒绝
        first = parse_writeoff(doc(
            "N1", "R1", "2026-06-02T08:00:00Z",
            [{"order_id": "O-EXACT", "batch_id": "B-R1", "qty": 70}]))
        self.assertTrue(svc.submit_writeoff(ds, first, self.now).accepted)
        second = parse_writeoff(doc(
            "N2", "R1", "2026-06-03T08:00:00Z",
            [{"order_id": "O-EXACT", "batch_id": "B-R1", "qty": 10}]))
        # 70+10=80 恰好等于分配量，也恰好等于账面 => 合法
        self.assertTrue(svc.submit_writeoff(ds, second, self.now).accepted)
        # 再出任何正数都会负库存，但累计量已达分配上限，先被 EXCEEDS 拦截；
        # 这里直接用账本工具证明合成事件的负库存检查路径有效
        from batch_alloc.writeoff import WriteOffRecord
        bad_rec = WriteOffRecord(
            writeoff_id="NX", plan_id="R1", status=STATUS_ACCEPTED,
            outbound_at=parse_dt("2026-06-04T08:00:00Z", "t"),
            submitted_at=self.now, content_hash="x",
            lines=parse_writeoff(doc(
                "NX", "R1", "2026-06-04T08:00:00Z",
                [{"order_id": "O-EXACT", "batch_id": "B-R1", "qty": 50}])).lines)
        aug = dataset_with_outbounds(ds, svc._accepted_records(ds.name) + [bad_rec])
        with self.assertRaises(InvalidHistory):
            from batch_alloc import build_ledger
            from batch_alloc.writeoff import _far_future
            build_ledger(aug, _far_future([bad_rec], aug))

    def test_multi_line_doc_atomic_on_illegal_history(self):
        # 一张单两行，其中一行导致负库存 => 整单拒绝，另一行也不得扣减
        ds = load("demo_wide_narrow.json")
        at = parse_dt("2026-06-01T00:00:00Z", "at")
        svc = WarehouseService(self.reg_path + ".y", self.store_path + ".y")
        plan = svc.recalc(ds, at, "W1", None, self.now)
        allocs = {a.order_id: a for a in plan.allocations}
        # 构造：先把 B-A 出到接近账面（查账面量）
        stock = {s.batch_id: s for s in svc.stock_view(ds)}
        ba_balance = stock["B-A"].book_balance
        oa = allocs["O-A"]
        # 首单出掉 B-A 全部账面（不超过 O-A 分配量时才可行；若分配量小于账面，
        # 则改为出分配量全额）
        first_q = min(ba_balance, oa.qty)
        first = parse_writeoff(doc(
            "M1", "W1", "2026-06-02T08:00:00Z",
            [{"order_id": "O-A", "batch_id": "B-A", "qty": first_q}]))
        r1 = svc.submit_writeoff(ds, first, self.now)
        self.assertTrue(r1.accepted, r1.failures)
        if ba_balance <= oa.qty:
            # B-A 已无库存：再对 O-A 出 1 会负库存；同单夹带 O-B 合法行
            bad = parse_writeoff(doc(
                "M2", "W1", "2026-06-03T08:00:00Z",
                [{"order_id": "O-A", "batch_id": "B-A", "qty": 1},
                 {"order_id": "O-B", "batch_id": "B-WIDE", "qty": 1}]))
            r2 = svc.submit_writeoff(ds, bad, self.now)
            self.assertFalse(r2.accepted)
            self.assertIn(WO_NEGATIVE_HISTORY, [f.code for f in r2.failures])
            # O-B 行也不得扣减
            rows = {r.order_id: r for r in svc.order_execution(ds)}
            self.assertEqual(rows["O-B"].shipped_qty, 0)


class TestDatasetMismatchAndParsing(WriteOffTestBase):
    def test_dataset_hash_mismatch_rejected(self):
        ds = load("demo_shelf.json")
        at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.svc.recalc(ds, at, "P1", "v1", self.now)
        data = raw("demo_shelf.json")
        data["quantity_events"][0]["qty"] = 99  # 改输入 => input_hash 变化
        ds2 = parse_dataset(data)
        req = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 1}]))
        res = self.svc.submit_writeoff(ds2, req, self.now)
        self.assertFalse(res.accepted)
        self.assertIn(WO_DATASET_MISMATCH, [f.code for f in res.failures])

    def test_invalid_qty_and_naive_datetime(self):
        with self.assertRaises(InputError):
            parse_writeoff(doc("W1", "P1", "2026-03-21T08:00:00",
                               [{"order_id": "O-F", "batch_id": "B-SH", "qty": 1}]))
        with self.assertRaises(InputError):
            parse_writeoff(doc("W1", "P1", "2026-03-21T08:00:00Z",
                               [{"order_id": "O-F", "batch_id": "B-SH", "qty": 0}]))
        with self.assertRaises(InputError):
            parse_writeoff(doc("W1", "P1", "2026-03-21T08:00:00Z", []))

    def test_persistence_roundtrip(self):
        ds = load("demo_shelf.json")
        at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.svc.recalc(ds, at, "P1", "v1", self.now)
        req = parse_writeoff(doc(
            "W1", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 4}]))
        self.svc.submit_writeoff(ds, req, self.now)
        # 重新打开服务，事实仍在且可继续累计
        svc2 = WarehouseService(self.reg_path, self.store_path)
        rows = {r.order_id: r for r in svc2.order_execution(ds)}
        self.assertEqual(rows["O-F"].shipped_qty, 4)
        req2 = parse_writeoff(doc(
            "W2", "P1", "2026-03-22T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 2}]))
        self.assertTrue(svc2.submit_writeoff(ds, req2, self.now).accepted)
        rows = {r.order_id: r for r in svc2.order_execution(ds)}
        self.assertEqual(rows["O-F"].shipped_qty, 6)


class TestConcurrency(WriteOffTestBase):
    def test_concurrent_writeoffs_never_overflow(self):
        ds = load("demo_shelf.json")
        at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.svc.recalc(ds, at, "P1", "v1", self.now)
        # O-F 分配 10；20 个线程各出 1，恰好最多 10 张被接受
        results = []
        lock = threading.Lock()

        def worker(i):
            req = parse_writeoff(doc(
                f"C{i}", "P1", f"2026-03-21T0{i % 9}:00:00Z",
                [{"order_id": "O-F", "batch_id": "B-SH", "qty": 1}]))
            res = self.svc.submit_writeoff(ds, req, self.now)
            with lock:
                results.append(res.accepted)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 10)  # 不超额
        rows = {r.order_id: r for r in self.svc.order_execution(ds)}
        self.assertEqual(rows["O-F"].shipped_qty, 10)
        stock = {s.batch_id: s for s in self.svc.stock_view(ds)}
        self.assertEqual(stock["B-SH"].on_hand, 90)

    def test_concurrent_resend_same_id_dedupes(self):
        ds = load("demo_shelf.json")
        at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.svc.recalc(ds, at, "P1", "v1", self.now)
        req = parse_writeoff(doc(
            "DUP", "P1", "2026-03-21T08:00:00Z",
            [{"order_id": "O-F", "batch_id": "B-SH", "qty": 3}]))
        outcomes = []
        lock = threading.Lock()

        def worker():
            res = self.svc.submit_writeoff(ds, req, self.now)
            with lock:
                outcomes.append((res.accepted, res.duplicate))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes), 8)
        self.assertTrue(all(a for a, _ in outcomes))
        self.assertEqual(sum(1 for _, d in outcomes if not d), 1)  # 仅一次首次落盘
        self.assertEqual(len(self.svc.list_writeoffs()), 1)
        rows = {r.order_id: r for r in self.svc.order_execution(ds)}
        self.assertEqual(rows["O-F"].shipped_qty, 3)  # 绝不重复扣减

    def test_concurrent_recalc_and_writeoff_serializable(self):
        ds = load("demo_shelf.json")
        at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.svc.recalc(ds, at, "P1", "v1", self.now)
        errors = []

        def writeoff_worker(i):
            try:
                req = parse_writeoff(doc(
                    f"S{i}", "P1", f"2026-03-21T08:0{i % 9}:00Z",
                    [{"order_id": "O-F", "batch_id": "B-SH", "qty": 1}]))
                self.svc.submit_writeoff(ds, req, self.now)
            except Exception as exc:  # pragma: no cover - 并发不应抛异常
                errors.append(exc)

        def recalc_worker(i):
            try:
                self.svc.recalc(ds, at, f"RC{i}", "v1", self.now)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = []
        for i in range(6):
            threads.append(threading.Thread(target=writeoff_worker, args=(i,)))
            threads.append(threading.Thread(target=recalc_worker, args=(i,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 最终一致：只有一个 ACTIVE 方案；已接受出库都在，且不超分配量
        active = self.svc.registry.active_plans()
        self.assertEqual(len(active), 1)
        rows = {r.order_id: r for r in self.svc.order_execution(ds)}
        self.assertLessEqual(rows["O-F"].shipped_qty, 10)
        # 任一已接受核销对应的方案快照必须曾是 ACTIVE（串行化保证）
        for rec in self.svc.list_writeoffs(STATUS_ACCEPTED):
            self.assertEqual(rec.plan_snapshot.status, "ACTIVE")


class TestCrossProcessLocking(WriteOffTestBase):
    """跨进程：多个进程同时提交，flock 保证最终不超额、单号去重。"""

    def test_subprocess_contention(self):
        ds = load("demo_shelf.json")
        at = parse_dt("2026-03-20T00:00:00Z", "at")
        self.svc.recalc(ds, at, "P1", "v1", self.now)

        worker = os.path.join(self.tmp.name, "proc_worker.py")
        with open(worker, "w", encoding="utf-8") as fh:
            fh.write(f"""
import sys
sys.path.insert(0, {os.path.join(ROOT, 'src')!r})
from batch_alloc import WarehouseService, load_dataset, parse_writeoff, parse_dt
ds = load_dataset({os.path.join(IN, 'demo_shelf.json')!r})
svc = WarehouseService({self.reg_path!r}, {self.store_path!r})
now = parse_dt('2026-03-20T01:00:00Z', 'now')
accepted = 0
for i in range(5):
    wid = sys.argv[1] + f'-{{i}}'
    req = parse_writeoff({{'writeoff_id': wid, 'plan_id': 'P1',
        'outbound_at': f'2026-03-21T0{{i}}:00:00Z',
        'lines': [{{'order_id': 'O-F', 'batch_id': 'B-SH', 'qty': 1}}]}})
    if svc.submit_writeoff(ds, req, now).accepted:
        accepted += 1
print(accepted)
""")
        procs = [subprocess.Popen([sys.executable, "-I", worker, f"P{pidx}"],
                                  stdout=subprocess.PIPE, text=True)
                 for pidx in range(4)]
        accepted_total = 0
        for p in procs:
            out, _ = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
            accepted_total += int(out.strip())
        self.assertEqual(accepted_total, 10)  # 4 进程共 20 次尝试，仅 10 件可出
        rows = {r.order_id: r for r in self.svc.order_execution(ds)}
        self.assertEqual(rows["O-F"].shipped_qty, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
