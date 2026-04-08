"""Data Loader — reads Frappe master data and converts to solver input types.

Loads FP TAT Master, FP Setup Matrix, FP Shift Calendar, and ERPNext
BOM/Routing to build Job / Operation objects for the solver engine.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import frappe

from fp.solver.engine import Job, Operation, SolverConfig


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_solver_inputs(
    demand_jobs: list[dict],
    horizon_start: date,
    horizon_end: date,
) -> tuple[list[Job], list[str], dict, dict]:
    """Load all master data and convert demand jobs to solver inputs.

    Args:
        demand_jobs: Output of ``build_demand_profile`` — list of dicts with
            keys: job_id, item_code, qty, due_date, lot_number, …
        horizon_start: Planning horizon start date.
        horizon_end: Planning horizon end date.

    Returns:
        (jobs, workstations, setup_matrix, shift_capacity)
    """
    tat_map = _load_tat_master()
    setup_matrix = _load_setup_matrix()
    shift_capacity = _load_shift_capacity(horizon_start, horizon_end)
    setup_group_map = _load_setup_group_map()
    routing_map = _load_routing_sequences()

    workstations = sorted(shift_capacity.keys())

    horizon_start_dt = datetime.combine(horizon_start, datetime.min.time())

    jobs: list[Job] = []
    for d in demand_jobs:
        if d.get("is_frozen"):
            continue

        item_code = d["item_code"]
        setup_group = setup_group_map.get(item_code, "")
        due_date_dt = _parse_date(d["due_date"])
        due_date_mins = _date_to_minutes(due_date_dt, horizon_start_dt)

        operations = _build_operations(item_code, tat_map, routing_map, workstations)

        jobs.append(Job(
            job_id=d["job_id"],
            item_code=item_code,
            qty=d["qty"],
            due_date_mins=max(0, due_date_mins),
            setup_group=setup_group,
            operations=operations,
        ))

    return jobs, workstations, setup_matrix, shift_capacity


def load_solver_config_from_doctype() -> SolverConfig:
    """Read FP Solver Config doctype and return a SolverConfig dataclass."""
    from fp.factory_planner.doctype.fp_solver_config.fp_solver_config import (
        get_solver_config,
    )

    cfg = get_solver_config()
    return SolverConfig(
        alpha=cfg["alpha"],
        beta=cfg["beta"],
        max_time_secs=cfg["max_time_secs"],
        num_workers=cfg["num_workers"],
        enable_scip_ensemble=cfg["enable_scip_ensemble"],
        scip_max_time_secs=cfg["scip_max_time_secs"],
        quality_threshold=cfg["quality_threshold"],
    )


def build_master_snapshot(
    horizon_start: date,
    horizon_end: date,
) -> dict:
    """Build a JSON-serializable snapshot of all master data used by the solver.

    Stored in FP Planning Snapshot.master_snapshot for auditability.
    """
    return {
        "generated_at": frappe.utils.now(),
        "horizon_start": str(horizon_start),
        "horizon_end": str(horizon_end),
        "tat_master": _load_tat_master_raw(),
        "setup_matrix": _load_setup_matrix_raw(),
        "shift_calendar": _load_shift_calendar_raw(horizon_start, horizon_end),
    }


# ---------------------------------------------------------------------------
# Internal loaders
# ---------------------------------------------------------------------------


def _load_tat_master() -> dict[tuple[str, str], dict]:
    """Load FP TAT Master → dict keyed by (item_code, operation)."""
    rows = frappe.get_all(
        "FP TAT Master",
        fields=[
            "item_code", "operation", "workstation",
            "base_tat_mins", "wait_time_mins",
            "is_inline_inspection", "inspection_tat_mins",
        ],
    )

    result: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["item_code"], r["operation"])
        result[key] = {
            "workstation": r.get("workstation") or "",
            "base_tat_mins": int(r.get("base_tat_mins") or 0),
            "wait_time_mins": int(r.get("wait_time_mins") or 0),
            "is_inline_inspection": bool(r.get("is_inline_inspection")),
            "inspection_tat_mins": int(r.get("inspection_tat_mins") or 0),
        }
    return result


def _load_tat_master_raw() -> list[dict]:
    """Raw TAT Master rows for snapshot."""
    return frappe.get_all(
        "FP TAT Master",
        fields=[
            "item_code", "operation", "workstation",
            "base_tat_mins", "wait_time_mins",
            "is_inline_inspection", "inspection_tat_mins",
        ],
    )


def _load_setup_matrix() -> dict[tuple[str, str, str], int]:
    """Load FP Setup Matrix → dict keyed by (workstation, from_group, to_group)."""
    rows = frappe.get_all(
        "FP Setup Matrix",
        fields=["workstation", "from_setup_group", "to_setup_group", "setup_time_mins"],
        filters={"is_transition_allowed": 1},
    )

    result: dict[tuple[str, str, str], int] = {}
    for r in rows:
        key = (r["workstation"], r["from_setup_group"], r["to_setup_group"])
        result[key] = int(r["setup_time_mins"])
    return result


def _load_setup_matrix_raw() -> list[dict]:
    """Raw Setup Matrix rows for snapshot."""
    return frappe.get_all(
        "FP Setup Matrix",
        fields=[
            "workstation", "from_setup_group", "to_setup_group",
            "setup_time_mins", "is_transition_allowed",
        ],
    )


def _load_shift_capacity(
    horizon_start: date,
    horizon_end: date,
) -> dict[str, int]:
    """Load FP Shift Calendar and sum available capacity per workstation.

    Returns dict mapping workstation name to total available minutes
    across the planning horizon.
    """
    rows = frappe.get_all(
        "FP Shift Calendar",
        fields=["workstation", "available_capacity_mins"],
        filters={
            "date": ["between", [str(horizon_start), str(horizon_end)]],
            "is_holiday": 0,
        },
    )

    capacity: dict[str, int] = {}
    for r in rows:
        ws = r["workstation"]
        mins = int(r.get("available_capacity_mins") or 0)
        capacity[ws] = capacity.get(ws, 0) + mins
    return capacity


def _load_shift_calendar_raw(
    horizon_start: date,
    horizon_end: date,
) -> list[dict]:
    """Raw Shift Calendar rows for snapshot."""
    return frappe.get_all(
        "FP Shift Calendar",
        fields=[
            "workstation", "date", "shift_type",
            "start_time", "end_time", "break_duration_mins",
            "is_holiday", "available_capacity_mins",
        ],
        filters={
            "date": ["between", [str(horizon_start), str(horizon_end)]],
        },
    )


def _load_setup_group_map() -> dict[str, str]:
    """Build item_code → setup_group_name lookup from FP Setup Group child table."""
    rows = frappe.get_all(
        "FP Setup Group Item",
        fields=["item_code", "parent"],
    )
    return {r["item_code"]: r["parent"] for r in rows}


def _load_routing_sequences() -> dict[str, list[dict]]:
    """Load operation sequences from ERPNext BOM Routing (BOM Operation table).

    Returns dict mapping item_code → sorted list of operation dicts.
    Falls back to empty if BOM/Routing is not configured.
    """
    try:
        rows = frappe.db.sql(
            """
            SELECT
                bom.item AS item_code,
                bo.operation,
                bo.sequence_id AS sequence
            FROM `tabBOM Operation` bo
            JOIN `tabBOM` bom ON bom.name = bo.parent
            WHERE bom.is_active = 1
              AND bom.is_default = 1
              AND bom.docstatus = 1
            ORDER BY bom.item, bo.sequence_id
            """,
            as_dict=True,
        )
    except Exception:
        return {}

    result: dict[str, list[dict]] = {}
    for r in rows:
        result.setdefault(r["item_code"], []).append({
            "operation": r["operation"],
            "sequence": int(r["sequence"]),
        })
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_operations(
    item_code: str,
    tat_map: dict[tuple[str, str], dict],
    routing_map: dict[str, list[dict]],
    workstations: list[str],
) -> list[Operation]:
    """Build Operation list for a given item using TAT Master + BOM Routing."""
    routing = routing_map.get(item_code, [])

    if not routing:
        # Fall back: use all TAT Master entries for this item, sorted by operation name
        item_tats = sorted(
            [(k, v) for k, v in tat_map.items() if k[0] == item_code],
            key=lambda x: x[0][1],
        )
        seq = 10
        operations = []
        for (_, op_name), tat in item_tats:
            ws = tat["workstation"]
            if not ws and workstations:
                ws = workstations[0]
            operations.append(Operation(
                name=op_name,
                sequence=seq,
                tat_mins=tat["base_tat_mins"],
                wait_time_mins=tat["wait_time_mins"],
                is_inline_inspection=tat["is_inline_inspection"],
                inspection_tat_mins=tat["inspection_tat_mins"],
                workstation=ws,
            ))
            seq += 10
        return operations

    # Use BOM routing order with TAT Master enrichment
    operations = []
    for route in routing:
        op_name = route["operation"]
        seq = route["sequence"]
        tat = tat_map.get((item_code, op_name), {})

        ws = tat.get("workstation", "")
        if not ws and workstations:
            ws = workstations[0]

        operations.append(Operation(
            name=op_name,
            sequence=seq,
            tat_mins=tat.get("base_tat_mins", 0),
            wait_time_mins=tat.get("wait_time_mins", 0),
            is_inline_inspection=tat.get("is_inline_inspection", False),
            inspection_tat_mins=tat.get("inspection_tat_mins", 0),
            workstation=ws,
        ))
    return operations


def _parse_date(value) -> datetime:
    """Parse a date string or date/datetime object to datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return datetime.strptime(str(value), "%Y-%m-%d")


def _date_to_minutes(target: datetime, horizon_start: datetime) -> int:
    """Convert a target datetime to minutes from horizon start."""
    delta = target - horizon_start
    return int(delta.total_seconds() / 60)
