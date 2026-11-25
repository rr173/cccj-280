"""batch_alloc：按规则版本与业务时点的批次可执行分配。"""
from .model import (
    Dataset, Batch, Order, ShelfRule, QuantityEvent, StatusEvent,
    QEventType, QStatus, AllocError, InputError, InvalidHistory,
    parse_dataset, load_dataset, parse_dt, to_plain,
)
from .engine import (
    Engine, build_ledger, latest_status, select_rule, expiry_datetime,
    LedgerResult, Eligibility, REASON_TEXT,
)
from .planner import solve, brute_force_verify, PlanResult, Allocation, BlockedOrder
from .registry import PlanRegistry, verify_plan, RegistryError
from .report import render_plan

__all__ = [
    "Dataset", "Batch", "Order", "ShelfRule", "QuantityEvent", "StatusEvent",
    "QEventType", "QStatus", "AllocError", "InputError", "InvalidHistory",
    "parse_dataset", "load_dataset", "parse_dt", "to_plain",
    "Engine", "build_ledger", "latest_status", "select_rule", "expiry_datetime",
    "LedgerResult", "Eligibility", "REASON_TEXT",
    "solve", "brute_force_verify", "PlanResult", "Allocation", "BlockedOrder",
    "PlanRegistry", "verify_plan", "RegistryError", "render_plan",
]
