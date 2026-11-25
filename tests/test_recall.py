"""只读召回清单单元测试：python -I tests/test_recall.py

覆盖：FIFO 后入库不冲抵较早发出、加工传递（全部产出受影响 / 更早产出不受影响）、
多路加工汇合只算一次、退回分次与超退、未入账退回不计、错误码、十亿级数量、
只读性（不改账本/核销/登记册）、命令行与 JSON 输出。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from batch_alloc import (  # noqa: E402
    parse_dataset, parse_dt, parse_recall_input, recall_report,
    RecallError, InputError,
)
from batch_alloc.writeoff import (  # noqa: E402
    WriteOffRecord, WriteOffLine, PlanSnapshot, STATUS_ACCEPTED,
    STATUS_REJECTED,
)

IN = os.path.join(ROOT, "sample_inputs", "recall")


def _base_dataset(**kw):
    """最小数据集：原料批 R / 产成品批 FG，各有若干事件；可覆盖。"""
    raw = {
        "name": kw.get("name", "t"),
        "batches": [
            {"batch_id": "R", "production_date": "2026-01-01", "purposes": ["F"],
             "initial_status": "QUARANTINED"},
            {"batch_id": "FG", "production_date": "2026-01-01", "purposes": ["F"],
             "initial_status": "QUARANTINED"},
        ],
        "quantity_events": [
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
        ],
        "status_events": [],
        "shelf_rules": [
            {"rule_id": "G", "purpose": None, "version": "v1",
             "effective_at": "2025-01-01T00:00:00Z", "shelf_life_days": 720,
             "timezone": "UTC"}],
        "orders": [],
    }
    for k, v in kw.items():
        if k != "name":
            raw[k] = v
    return parse_dataset(raw)


def _rec(wid, at, lines, dataset="t", status=STATUS_ACCEPTED, plan="P"):
    snap = PlanSnapshot(plan, "ACTIVE", dataset, "2026-01-01T00:00:00+00:00",
                        None, "h", [])
    return WriteOffRecord(
        writeoff_id=wid, plan_id=plan, status=status,
        outbound_at=parse_dt(at, "at"), submitted_at=parse_dt(at, "at"),
        content_hash="x" * 16,
        lines=[WriteOffLine(*ln) for ln in lines],
        plan_snapshot=snap if status == STATUS_ACCEPTED else None,
    )


def _ri(sources, processes=(), returns=()):
    return parse_recall_input({
        "recall_source_event_ids": list(sources),
        "processes": [{"link_id": l, "issue_events": list(i), "output_events": list(o)}
                      for l, i, o in processes],
        "returns": [{"return_event_id": e, "writeoff_id": w, "order_id": o,
                     "batch_id": b} for e, w, o, b in returns],
    })


AS_OF = parse_dt("2026-12-31T00:00:00Z", "as_of")


class TestLedgerOrder(unittest.TestCase):
    def test_later_receipt_not_consumed_by_earlier_issue(self):
        # 先领用 80（当时只有 R-1），后入库 R-2=50；再领用 40 只能吃 R-1 余 20 + R-2 20
        # => 剩余 30 全为无来源 R-2，较早发出不会“借用”后入库货物
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 80,
             "effective_at": "2026-01-15T00:00:00Z"},
            {"event_id": "R-2", "batch_id": "R", "type": "RECEIPT", "qty": 50,
             "effective_at": "2026-02-01T00:00:00Z"},
            {"event_id": "I-2", "batch_id": "R", "type": "ISSUE", "qty": 40,
             "effective_at": "2026-02-10T00:00:00Z"},
        ])
        rep = recall_report(ds, [], _ri(["R-1"]), AS_OF)
        # 剩余 30 全是后入库的无来源 R-2，批次不出现在受影响在库清单
        self.assertEqual(rep["affected_stock"], [])

    def test_issue_before_later_receipt_cannot_overdraw(self):
        # 账本顺序下领用 120 时仅 100 在库 => 非法历史，报错而非借用后入库货物
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "R-2", "batch_id": "R", "type": "RECEIPT", "qty": 50,
             "effective_at": "2026-02-01T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 120,
             "effective_at": "2026-01-15T00:00:00Z"},
        ])
        with self.assertRaises(RecallError) as cm:
            recall_report(ds, [], _ri(["R-1"]), AS_OF)
        self.assertEqual(cm.exception.code, "RC_NEGATIVE_BALANCE")

    def test_issue_spanning_sources_and_partial_stock(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 30,
             "effective_at": "2026-01-15T00:00:00Z"},
        ])
        rep = recall_report(ds, [], _ri(["R-1"]), AS_OF)
        self.assertEqual(rep["affected_stock"][0]["affected_qty"], 70)

    def test_as_of_cuts_events(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 30,
             "effective_at": "2026-06-15T00:00:00Z"},
        ])
        at = parse_dt("2026-03-01T00:00:00Z", "at")
        rep = recall_report(ds, [], _ri(["R-1"]), at)
        self.assertEqual(rep["affected_stock"][0]["affected_qty"], 100)

    def test_scrap_follows_fifo(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "S-1", "batch_id": "R", "type": "SCRAP", "qty": 40,
             "effective_at": "2026-01-05T00:00:00Z"},
        ])
        rep = recall_report(ds, [], _ri(["R-1"]), AS_OF)
        self.assertEqual(rep["affected_stock"][0]["affected_qty"], 60)


class TestWriteoff(unittest.TestCase):
    def test_later_writeoff_not_counted_before_as_of(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
        ])
        rec = _rec("W1", "2026-05-01T00:00:00Z", [("O", "R", 20)])
        at = parse_dt("2026-03-01T00:00:00Z", "at")
        rep = recall_report(ds, [rec], _ri(["R-1"]), at)
        self.assertEqual(rep["affected_orders"], [])
        self.assertEqual(rep["affected_stock"][0]["affected_qty"], 100)

    def test_multi_line_writeoff_ordered_by_order_id(self):
        # 同单同批两行：O-A 10, O-B 20；FIFO 20 件来源，O-A 先取 10 有来源，O-B 取 10 有来源+10 无
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 20,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "R-2", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-03T00:00:00Z"},
        ])
        rec = _rec("W1", "2026-02-01T00:00:00Z",
                   [("O-B", "R", 20), ("O-A", "R", 10)])   # 输入顺序无关
        rep = recall_report(ds, [rec], _ri(["R-1"]), AS_OF)
        rows = {r["order_id"]: r for r in rep["affected_orders"]}
        self.assertEqual(rows["O-A"]["qty"], 10)
        self.assertEqual(rows["O-B"]["qty"], 10)
        self.assertEqual(rows["O-B"]["unrecovered_qty"], 10)
        self.assertEqual(rows["O-B"]["by_source"], {"R-1": 10})

    def test_writeoff_consumes_fifo_so_stock_source_correct(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
        ])
        rec = _rec("W1", "2026-02-01T00:00:00Z", [("O", "R", 40)])
        rep = recall_report(ds, [rec], _ri(["R-1"]), AS_OF)
        self.assertEqual(rep["affected_orders"][0]["qty"], 40)
        self.assertEqual(rep["affected_stock"][0]["affected_qty"], 60)

    def test_rejected_writeoff_not_counted(self):
        ds = _base_dataset()
        rec = _rec("W1", "2026-02-01T00:00:00Z", [("O", "R", 20)], status=STATUS_REJECTED)
        rec.plan_snapshot = PlanSnapshot("P", "ACTIVE", "t", "x", None, "h", [])
        rep = recall_report(ds, [rec], _ri(["R-1"]), AS_OF)
        self.assertEqual(rep["affected_orders"], [])
        self.assertEqual(rep["affected_stock"][0]["affected_qty"], 100)


class TestProcessLink(unittest.TestCase):
    def _proc_ds(self):
        return _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "R-2", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-03T00:00:00Z"},
            # 更早的产出（不受本次加工影响）
            {"event_id": "P-0", "batch_id": "FG", "type": "RECEIPT", "qty": 5,
             "effective_at": "2026-01-04T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 30,
             "effective_at": "2026-01-10T00:00:00Z"},
            {"event_id": "P-1", "batch_id": "FG", "type": "RECEIPT", "qty": 12,
             "effective_at": "2026-01-11T00:00:00Z"},
            {"event_id": "P-2", "batch_id": "FG", "type": "RECEIPT", "qty": 8,
             "effective_at": "2026-01-12T00:00:00Z"},
        ])

    def test_all_outputs_affected_and_earlier_output_not(self):
        ds = self._proc_ds()
        rep = recall_report(ds, [], _ri(["R-1"], [("L", ["I-1"], ["P-1", "P-2"])]), AS_OF)
        stock = {r["batch_id"]: r for r in rep["affected_stock"]}
        self.assertEqual(stock["FG"]["on_hand"], 25)
        self.assertEqual(stock["FG"]["affected_qty"], 20)     # 12+8，不含更早的 5
        self.assertEqual(stock["R"]["affected_qty"], 70)      # 100-30 未领用

    def test_process_not_using_source_has_no_effect(self):
        ds = self._proc_ds()
        # 只把无来源的 I（R-2 段）关联：先领用 100 使第二次领用落在 R-2
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "R-2", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-03T00:00:00Z"},
            {"event_id": "I-0", "batch_id": "R", "type": "ISSUE", "qty": 100,
             "effective_at": "2026-01-05T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 30,
             "effective_at": "2026-01-10T00:00:00Z"},
            {"event_id": "P-1", "batch_id": "FG", "type": "RECEIPT", "qty": 12,
             "effective_at": "2026-01-11T00:00:00Z"},
        ])
        rep = recall_report(ds, [], _ri(["R-1"], [("L", ["I-1"], ["P-1"])]), AS_OF)
        self.assertEqual(rep["affected_stock"], [])   # 剩余全是无来源 R-2，FG 也无来源

    def test_multi_path_convergence_counted_once(self):
        # 两个加工都消耗召回来源，产出汇入同一批 FG；按件只算一次
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 20,
             "effective_at": "2026-01-10T00:00:00Z"},
            {"event_id": "I-2", "batch_id": "R", "type": "ISSUE", "qty": 20,
             "effective_at": "2026-01-10T00:00:01Z"},
            {"event_id": "P-1", "batch_id": "FG", "type": "RECEIPT", "qty": 15,
             "effective_at": "2026-01-11T00:00:00Z"},
            {"event_id": "P-2", "batch_id": "FG", "type": "RECEIPT", "qty": 15,
             "effective_at": "2026-01-12T00:00:00Z"},
        ])
        rep = recall_report(ds, [], _ri(
            ["R-1"],
            [("L1", ["I-1"], ["P-1"]), ("L2", ["I-2"], ["P-2"])]), AS_OF)
        stock = {r["batch_id"]: r for r in rep["affected_stock"]}
        self.assertEqual(stock["FG"]["affected_qty"], 30)
        self.assertEqual(stock["FG"]["by_source"], {"R-1": 30})
        self.assertEqual(rep["totals"]["affected_stock"], 60 + 30)

    def test_in_transit_reported_until_output(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 30,
             "effective_at": "2026-01-10T00:00:00Z"},
            {"event_id": "P-1", "batch_id": "FG", "type": "RECEIPT", "qty": 12,
             "effective_at": "2026-02-10T00:00:00Z"},
        ])
        at = parse_dt("2026-02-01T00:00:00Z", "at")
        rep = recall_report(ds, [], _ri(["R-1"], [("L", ["I-1"], ["P-1"])]), at)
        self.assertEqual(rep["in_transit"][0]["qty"], 30)
        self.assertEqual(rep["affected_stock"][0]["affected_qty"], 70)

    def test_every_output_affected_even_if_output_small(self):
        # 只领用了 1 单位来源，产出 100 也全部视为受影响
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 1,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 1,
             "effective_at": "2026-01-10T00:00:00Z"},
            {"event_id": "P-1", "batch_id": "FG", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-11T00:00:00Z"},
        ])
        rep = recall_report(ds, [], _ri(["R-1"], [("L", ["I-1"], ["P-1"])]), AS_OF)
        stock = {r["batch_id"]: r for r in rep["affected_stock"]}
        self.assertEqual(stock["FG"]["affected_qty"], 100)


class TestReturnLink(unittest.TestCase):
    def _ds(self):
        return _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "RT-1", "batch_id": "R", "type": "RETURN", "qty": 30,
             "effective_at": "2026-03-01T00:00:00Z", "sealed": True, "accepted": True},
        ])

    def test_split_returns_recover_pieces(self):
        ds = self._ds()
        rec = _rec("W1", "2026-02-01T00:00:00Z", [("O", "R", 50)])
        rep = recall_report(ds, [rec], _ri(
            ["R-1"], returns=[("RT-1", "W1", "O", "R")]), AS_OF)
        self.assertEqual(rep["affected_orders"][0]["unrecovered_qty"], 20)
        self.assertEqual(rep["recovered"][0]["qty"], 30)
        stock = {r["batch_id"]: r for r in rep["affected_stock"]}
        # 在库：100 - 50 + 30 = 80，全部含来源
        self.assertEqual(stock["R"]["on_hand"], 80)
        self.assertEqual(stock["R"]["affected_qty"], 80)

    def test_cumulative_return_over_limit_errors(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "RT-1", "batch_id": "R", "type": "RETURN", "qty": 4,
             "effective_at": "2026-03-01T00:00:00Z", "sealed": True, "accepted": True},
            {"event_id": "RT-2", "batch_id": "R", "type": "RETURN", "qty": 4,
             "effective_at": "2026-03-02T00:00:00Z", "sealed": True, "accepted": True},
        ])
        rec = _rec("W1", "2026-02-01T00:00:00Z", [("O", "R", 5)])
        with self.assertRaises(RecallError) as cm:
            recall_report(ds, [rec], _ri(
                ["R-1"], returns=[("RT-1", "W1", "O", "R"), ("RT-2", "W1", "O", "R")]), AS_OF)
        self.assertEqual(cm.exception.code, "RC_RETURN_OVER")

    def test_missing_return_link_errors(self):
        ds = self._ds()
        rec = _rec("W1", "2026-02-01T00:00:00Z", [("O", "R", 50)])
        with self.assertRaises(RecallError) as cm:
            recall_report(ds, [rec], _ri(["R-1"]), AS_OF)
        self.assertEqual(cm.exception.code, "RC_MISSING_RETURN_LINK")

    def test_unaccounted_return_no_link_needed(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 100,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "RT-1", "batch_id": "R", "type": "RETURN", "qty": 10,
             "effective_at": "2026-03-01T00:00:00Z", "sealed": False, "accepted": True},
        ])
        rep = recall_report(ds, [], _ri(["R-1"]), AS_OF)
        self.assertEqual(rep["recovered"], [])

    def test_return_target_not_yet_occurred_errors(self):
        ds = self._ds()
        rec = _rec("W1", "2026-02-01T00:00:00Z", [("O", "R", 50)])
        bad = _ri(["R-1"], returns=[("RT-1", "W9", "O", "R")])
        with self.assertRaises(RecallError) as cm:
            recall_report(ds, [rec], bad, AS_OF)
        self.assertEqual(cm.exception.code, "RC_RETURN_NO_TARGET")


class TestErrorCodes(unittest.TestCase):
    def test_missing_return_unknown_event(self):
        ds = _base_dataset()
        with self.assertRaises(RecallError) as cm:
            recall_report(ds, [], _ri(["NOPE"]), AS_OF)
        self.assertEqual(cm.exception.code, "RC_INVALID_LINK")

    def test_source_is_output_errors(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 10,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 5,
             "effective_at": "2026-01-10T00:00:00Z"},
            {"event_id": "P-1", "batch_id": "FG", "type": "RECEIPT", "qty": 5,
             "effective_at": "2026-01-11T00:00:00Z"},
        ])
        with self.assertRaises(RecallError) as cm:
            recall_report(ds, [], _ri(["P-1"], [("L", ["I-1"], ["P-1"])]), AS_OF)
        self.assertEqual(cm.exception.code, "RC_EVENT_MISMATCH")

    def test_output_before_issue_errors(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 10,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 5,
             "effective_at": "2026-02-10T00:00:00Z"},
            {"event_id": "P-1", "batch_id": "FG", "type": "RECEIPT", "qty": 5,
             "effective_at": "2026-01-11T00:00:00Z"},
        ])
        with self.assertRaises(RecallError) as cm:
            recall_report(ds, [], _ri(["R-1"], [("L", ["I-1"], ["P-1"])]), AS_OF)
        self.assertEqual(cm.exception.code, "RC_INVALID_LINK")

    def test_event_in_two_processes_errors(self):
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": 10,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 5,
             "effective_at": "2026-01-10T00:00:00Z"},
            {"event_id": "P-1", "batch_id": "FG", "type": "RECEIPT", "qty": 3,
             "effective_at": "2026-01-11T00:00:00Z"},
            {"event_id": "P-2", "batch_id": "FG", "type": "RECEIPT", "qty": 2,
             "effective_at": "2026-01-12T00:00:00Z"},
        ])
        with self.assertRaises(RecallError) as cm:
            recall_report(ds, [], _ri(
                ["R-1"], [("L1", ["I-1"], ["P-1"]), ("L2", ["I-1"], ["P-2"])]), AS_OF)
        self.assertEqual(cm.exception.code, "RC_DUPLICATE_EVENT")


class TestScale(unittest.TestCase):
    def test_billion_quantity_no_expansion(self):
        n = 1_000_000_000
        ds = _base_dataset(quantity_events=[
            {"event_id": "R-1", "batch_id": "R", "type": "RECEIPT", "qty": n,
             "effective_at": "2026-01-02T00:00:00Z"},
            {"event_id": "I-0", "batch_id": "R", "type": "ISSUE", "qty": 100_000_000,
             "effective_at": "2026-01-05T00:00:00Z"},
            {"event_id": "I-1", "batch_id": "R", "type": "ISSUE", "qty": 400_000_000,
             "effective_at": "2026-01-10T00:00:00Z"},
            {"event_id": "P-1", "batch_id": "FG", "type": "RECEIPT", "qty": n,
             "effective_at": "2026-01-11T00:00:00Z"},
        ])
        rep = recall_report(ds, [], _ri(["R-1"], [("L", ["I-1"], ["P-1"])]), AS_OF)
        stock = {r["batch_id"]: r for r in rep["affected_stock"]}
        self.assertEqual(stock["R"]["affected_qty"], 500_000_000)
        self.assertEqual(stock["FG"]["affected_qty"], 1_000_000_000)
        self.assertEqual(rep["totals"]["affected_stock"], 1_500_000_000)


class TestReadOnly(unittest.TestCase):
    def test_does_not_mutate_dataset_or_store(self):
        ds = _base_dataset()
        before = json.dumps([e.event_id for e in ds.quantity_events])
        recs = [_rec("W1", "2026-02-01T00:00:00Z", [("O", "R", 10)])]
        rec_before = repr(recs[0])
        recall_report(ds, recs, _ri(["R-1"]), AS_OF)
        self.assertEqual(json.dumps([e.event_id for e in ds.quantity_events]), before)
        self.assertEqual(repr(recs[0]), rec_before)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-I", os.path.join(ROOT, "src", "cli.py"), *args],
            capture_output=True, text=True, cwd=ROOT)

    def test_end_to_end_readonly_and_json(self):
        reg = os.path.join(self.tmp, "registry.json")
        store = os.path.join(self.tmp, "writeoffs.json")
        ds = os.path.join(IN, "recall_dataset.json")
        doc = os.path.join(IN, "recall_writeoff.json")
        r = self._run("plan", "--input", ds, "--as-of", "2026-02-15T00:00:00Z",
                      "--plan-id", "P-REC", "--registry", reg, "--no-bruteforce")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("writeoff", "--input", ds, "--doc", doc, "--registry", reg)
        self.assertEqual(r.returncode, 0, r.stderr)
        files = {f: hashlib.sha256(open(os.path.join(self.tmp, f), "rb").read()).hexdigest()
                 for f in os.listdir(self.tmp) if f.endswith(".json")}
        out = os.path.join(self.tmp, "recall_out.json")
        r = self._run("recall", "--input", ds, "--as-of", "2026-03-01T00:00:00Z",
                      "--recall", os.path.join(IN, "recall_input.json"),
                      "--registry", reg, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.load(open(out, encoding="utf-8"))
        self.assertEqual(rep["affected_orders"][0]["unrecovered_qty"], 10)
        stock = {x["batch_id"]: x for x in rep["affected_stock"]}
        self.assertEqual(stock["B-FG"]["affected_qty"], 40)
        self.assertEqual(stock["B-RAW"]["affected_qty"], 35)
        # 只读：核销存储与登记册未被改写
        after = {f: hashlib.sha256(open(os.path.join(self.tmp, f), "rb").read()).hexdigest()
                 for f in os.listdir(self.tmp) if f.endswith(".json") and f != "recall_out.json"}
        self.assertEqual(files, after)

    def test_error_exit_code(self):
        ds = os.path.join(IN, "recall_dataset.json")
        tmp_input = os.path.join(self.tmp, "bad.json")
        json.dump({"recall_source_event_ids": ["NOPE"]},
                  open(tmp_input, "w", encoding="utf-8"))
        r = self._run("recall", "--input", ds, "--as-of", "2026-03-01T00:00:00Z",
                      "--recall", tmp_input, "--store", os.path.join(self.tmp, "none.json"))
        self.assertEqual(r.returncode, 3)
        self.assertIn("RC_INVALID_LINK", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
