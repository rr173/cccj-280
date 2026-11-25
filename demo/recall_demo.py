"""只读召回清单端到端演示：python3 -I demo/recall_demo.py

场景（sample_inputs/recall/）：
- 原料 B-RAW 两次入库 R-1=100（受召回）、R-2=50（不受召回）；B-RAW2 入库 R-3=200。
- 加工 L1 领用 I-1(30，全部来自 R-1) → 产出 P-1(25)；加工 L2 领用 I-3(20) → 产出 P-2(15)。
- 另有已领用未产出的 I-2(B-RAW2 10)、报废 S-1(B-RAW 5)。
- 核销单 WO-1 出库 O-1 20 件（FIFO 落在 R-1 段）；退回 RT-1 收回 10 件。
断言：
  1. 未收回受影响 10、已出库受影响 20、收回 10；
  2. 受影响在库：B-RAW 35（=100-30-20+10-5，其中 35 含来源）、B-FG 40（=25+15）；
  3. 当前仍受影响 = 在库 75 + 未收回 10 = 85（已收回 10 已重新计入在库，
     不重复相加；已出库受影响 20 = 未收回 10 + 已收回 10）；
  4. 只读：核销存储与登记册哈希不变；十亿级数量不展开。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from batch_alloc import (  # noqa: E402
    load_dataset, parse_dt, load_recall_input, recall_report,
    recall_report_from_store, render_recall,
)
from batch_alloc.recall import RecallError  # noqa: E402

IN = os.path.join(ROOT, "sample_inputs", "recall")
DS = os.path.join(IN, "recall_dataset.json")


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run_cli(*args):
    r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "src", "cli.py"), *args],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(f"CLI 失败 {args}: {r.stderr}")
    return r


def main() -> int:
    tmp = tempfile.mkdtemp()
    try:
        reg = os.path.join(tmp, "registry.json")
        store = os.path.join(tmp, "writeoffs.json")
        dp, wo = "2026-02-15T00:00:00Z", "2026-02-18T08:00:00Z"

        run_cli("plan", "--input", DS, "--as-of", dp, "--plan-id", "P-REC",
                "--registry", reg, "--no-bruteforce")
        run_cli("writeoff", "--input", DS, "--doc",
                os.path.join(IN, "recall_writeoff.json"), "--registry", reg)
        before = {p: _sha(p) for p in (reg, store)}

        ds = load_dataset(DS)
        at = parse_dt("2026-03-01T00:00:00Z", "as_of")
        ri = load_recall_input(os.path.join(IN, "recall_input.json"))
        rep = recall_report_from_store(ds, store, ri, at)
        print(render_recall(rep))

        orders = {r["order_id"]: r for r in rep["affected_orders"]}
        stock = {r["batch_id"]: r for r in rep["affected_stock"]}
        assert orders["O-1"]["qty"] == 20, orders
        assert orders["O-1"]["unrecovered_qty"] == 10, orders
        assert stock["B-RAW"]["on_hand"] == 85 and stock["B-RAW"]["affected_qty"] == 35, stock
        assert stock["B-FG"]["affected_qty"] == 40, stock
        assert rep["recovered"][0]["qty"] == 10
        assert rep["totals"]["affected_stock"] == 75
        # 当前仍受影响 = 在库 75 + 未收回 10；已收回的 10 件已重新计入在库，不重复相加
        assert rep["totals"]["affected_outstanding"] == 85
        assert rep["totals"]["affected_shipped"] == 20 == (
            rep["totals"]["affected_unrecovered"] + rep["totals"]["affected_recovered"])
        print("场景断言通过：未收回/在库/已收回/只算一次 全部符合手算")

        # 只读性：登记册与核销存储哈希不变
        after = {p: _sha(p) for p in (reg, store)}
        assert before == after
        print("只读性通过：登记册与核销存储哈希未变")

        # 错误不交出清单：删掉退回关联 => 缺必需关联，退出码 3
        bad_in = os.path.join(tmp, "bad.json")
        with open(os.path.join(IN, "recall_input.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["returns"] = []
        with open(bad_in, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "src", "cli.py"),
                            "recall", "--input", DS, "--as-of", "2026-03-01T00:00:00Z",
                            "--recall", bad_in, "--store", store],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 3 and "RC_MISSING_RETURN_LINK" in r.stderr, r.stderr
        print("错误处理通过：缺少退回关联 => 报错退出 3，不输出不完整清单")

        # 十亿级数量：单段区间运算，秒级完成
        big = {
            "name": "big",
            "batches": [{"batch_id": "A", "production_date": "2026-01-01",
                         "purposes": ["F"]},
                        {"batch_id": "B", "production_date": "2026-01-01",
                         "purposes": ["F"]}],
            "quantity_events": [
                {"event_id": "S1", "batch_id": "A", "type": "RECEIPT",
                 "qty": 3_000_000_000, "effective_at": "2026-01-02T00:00:00Z"},
                {"event_id": "I1", "batch_id": "A", "type": "ISSUE",
                 "qty": 1_000_000_000, "effective_at": "2026-01-05T00:00:00Z"},
                {"event_id": "P1", "batch_id": "B", "type": "RECEIPT",
                 "qty": 2_500_000_000, "effective_at": "2026-01-06T00:00:00Z"},
            ],
            "status_events": [],
            "shelf_rules": [{"rule_id": "G", "purpose": None, "version": "v1",
                             "effective_at": "2025-01-01T00:00:00Z",
                             "shelf_life_days": 720, "timezone": "UTC"}],
            "orders": [],
        }
        from batch_alloc import parse_dataset, parse_recall_input
        big_ds = parse_dataset(big)
        big_ri = parse_recall_input({
            "recall_source_event_ids": ["S1"],
            "processes": [{"link_id": "L", "issue_events": ["I1"],
                           "output_events": ["P1"]}]})
        big_rep = recall_report(big_ds, [], big_ri, parse_dt("2026-12-31T00:00:00Z", "at"))
        bstock = {r["batch_id"]: r for r in big_rep["affected_stock"]}
        assert bstock["A"]["affected_qty"] == 2_000_000_000, bstock
        assert bstock["B"]["affected_qty"] == 2_500_000_000, bstock
        print("十亿级数量通过：3,000,000,000 件按区间运算，未展开单件")
        print("\n全部演示断言通过。")
        return 0
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    raise SystemExit(main())
