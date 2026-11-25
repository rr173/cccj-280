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
from .report import (
    render_plan, render_writeoff_result, render_writeoff_records,
    render_order_execution, render_stock,
)
from .writeoff import (
    WarehouseService, WriteOffStore, WriteOffRequest, WriteOffLine,
    WriteOffRecord, WriteOffResult, WriteOffFailure, PlanSnapshot,
    OrderExecution, BatchStock, parse_writeoff, load_writeoff,
    dataset_with_outbounds, content_hash_of,
    STATUS_ACCEPTED, STATUS_REJECTED, WO_REASON_TEXT,
    WO_PLAN_NOT_ACTIVE, WO_BATCH_MISMATCH, WO_EXCEEDS_ALLOCATED,
    WO_NEGATIVE_HISTORY, WO_IDEMPOTENT_CONFLICT, WO_LINE_NOT_IN_PLAN,
    WO_QTY_INVALID, WO_DUPLICATE_LINE, WO_PLAN_NOT_FOUND,
    WO_DATASET_MISMATCH, WO_LINE_UNKNOWN_ORDER, WO_DOC_EMPTY,
)

__all__ = [
    "Dataset", "Batch", "Order", "ShelfRule", "QuantityEvent", "StatusEvent",
    "QEventType", "QStatus", "AllocError", "InputError", "InvalidHistory",
    "parse_dataset", "load_dataset", "parse_dt", "to_plain",
    "Engine", "build_ledger", "latest_status", "select_rule", "expiry_datetime",
    "LedgerResult", "Eligibility", "REASON_TEXT",
    "solve", "brute_force_verify", "PlanResult", "Allocation", "BlockedOrder",
    "PlanRegistry", "verify_plan", "RegistryError", "render_plan",
    "render_writeoff_result", "render_writeoff_records",
    "render_order_execution", "render_stock",
    "WarehouseService", "WriteOffStore", "WriteOffRequest", "WriteOffLine",
    "WriteOffRecord", "WriteOffResult", "WriteOffFailure", "PlanSnapshot",
    "OrderExecution", "BatchStock", "parse_writeoff", "load_writeoff",
    "dataset_with_outbounds", "content_hash_of",
    "STATUS_ACCEPTED", "STATUS_REJECTED", "WO_REASON_TEXT",
    "WO_PLAN_NOT_ACTIVE", "WO_BATCH_MISMATCH", "WO_EXCEEDS_ALLOCATED",
    "WO_NEGATIVE_HISTORY", "WO_IDEMPOTENT_CONFLICT", "WO_LINE_NOT_IN_PLAN",
    "WO_QTY_INVALID", "WO_DUPLICATE_LINE", "WO_PLAN_NOT_FOUND",
    "WO_DATASET_MISMATCH", "WO_LINE_UNKNOWN_ORDER", "WO_DOC_EMPTY",
]
