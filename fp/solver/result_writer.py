"""Result Writer — persists solver output to FP Planning Snapshot.

Converts solver minute-based schedule back to absolute datetimes,
calculates KPIs, and creates the FP Planning Snapshot doctype with
FP Snapshot Job child rows and a master data JSON snapshot.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import frappe

from fp.solver.engine import SolverResult


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_snapshot(
    result: SolverResult,
    snapshot_name: str,
    horizon_start: date,
    horizon_end: date,
    master_snapshot: dict | None = None,
) -> str:
    """Create an FP Planning Snapshot from a SolverResult.

    Args:
        result: Completed SolverResult from the solver engine.
        snapshot_name: Human-readable name for this planning run.
        horizon_start: Planning horizon start date.
        horizon_end: Planning horizon end date.
        master_snapshot: Optional pre-built master data snapshot dict.

    Returns:
        The name (ID) of the created FP Planning Snapshot document.
    """
    horizon_start_dt = datetime.combine(horizon_start, datetime.min.time())

    utilization = _calculate_utilization(result, horizon_start, horizon_end)

    doc = frappe.new_doc("FP Planning Snapshot")
    doc.snapshot_name = snapshot_name
    doc.status = "Draft Plan"
    doc.planning_horizon_start = horizon_start
    doc.planning_horizon_end = horizon_end

    # KPIs
    doc.solver_run_time_secs = result.runtime_secs
    doc.objective_value = result.objective_value
    doc.total_tardiness_mins = result.total_tardiness_mins
    doc.total_setup_time_mins = result.total_setup_time_mins
    doc.line_utilization_pct = utilization

    # Approval
    doc.created_by_user = frappe.session.user

    # Scheduled jobs as child table rows
    for sched in result.scheduled_jobs:
        planned_start = _mins_to_datetime(sched["planned_start_mins"], horizon_start_dt)
        planned_end = _mins_to_datetime(sched["planned_end_mins"], horizon_start_dt)
        due_date_val = _mins_to_date(sched["due_date_mins"], horizon_start_dt)

        doc.append("jobs", {
            "job_id": sched["job_id"],
            "item_code": sched["item_code"],
            "qty": sched["qty"],
            "workstation": sched["workstation"],
            "operation": sched["operation"],
            "operation_sequence": sched["operation_sequence"],
            "planned_start": planned_start,
            "planned_end": planned_end,
            "setup_time_mins": sched.get("setup_time_mins", 0),
            "due_date": due_date_val,
            "tardiness_mins": sched.get("tardiness_mins", 0),
            "source_demand_id": sched.get("source_demand_id", ""),
            "is_frozen": 0,
        })

    # Master data snapshot JSON
    if master_snapshot:
        doc.master_snapshot = json.dumps(master_snapshot, default=str, ensure_ascii=False)

    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return doc.name


def update_snapshot_kpis(snapshot_name: str) -> None:
    """Recalculate and update KPIs on an existing snapshot."""
    doc = frappe.get_doc("FP Planning Snapshot", snapshot_name)

    total_tardiness = sum(row.tardiness_mins or 0 for row in doc.jobs)
    total_setup = sum(row.setup_time_mins or 0 for row in doc.jobs)

    doc.total_tardiness_mins = total_tardiness
    doc.total_setup_time_mins = total_setup
    doc.save(ignore_permissions=True)
    frappe.db.commit()


# ---------------------------------------------------------------------------
# KPI Calculations
# ---------------------------------------------------------------------------


def _calculate_utilization(
    result: SolverResult,
    horizon_start: date,
    horizon_end: date,
) -> float:
    """Calculate line utilization percentage.

    utilization = (total_processing_time / total_available_capacity) * 100
    """
    if not result.scheduled_jobs:
        return 0.0

    # Sum actual processing time per workstation
    ws_processing: dict[str, int] = {}
    for sched in result.scheduled_jobs:
        ws = sched["workstation"]
        duration = sched["planned_end_mins"] - sched["planned_start_mins"]
        ws_processing[ws] = ws_processing.get(ws, 0) + duration

    # Try to load shift capacity from DB for the horizon
    try:
        total_capacity = _get_total_capacity(horizon_start, horizon_end, list(ws_processing.keys()))
    except Exception:
        total_capacity = 0

    if total_capacity <= 0:
        # Fallback: use the maximum scheduled end time as proxy for capacity
        max_end = max(s["planned_end_mins"] for s in result.scheduled_jobs)
        num_ws = len(ws_processing)
        total_capacity = max_end * num_ws if num_ws > 0 else 1

    total_processing = sum(ws_processing.values())
    return round((total_processing / total_capacity) * 100, 2) if total_capacity > 0 else 0.0


def _get_total_capacity(
    horizon_start: date,
    horizon_end: date,
    workstations: list[str],
) -> int:
    """Sum available capacity from FP Shift Calendar for given workstations."""
    if not workstations:
        return 0

    rows = frappe.get_all(
        "FP Shift Calendar",
        fields=["sum(available_capacity_mins) as total"],
        filters={
            "date": ["between", [str(horizon_start), str(horizon_end)]],
            "is_holiday": 0,
            "workstation": ["in", workstations],
        },
    )
    return int(rows[0].get("total") or 0) if rows else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mins_to_datetime(minutes: int, horizon_start: datetime) -> datetime:
    """Convert solver minutes offset to absolute datetime."""
    return horizon_start + timedelta(minutes=minutes)


def _mins_to_date(minutes: int, horizon_start: datetime) -> date:
    """Convert solver minutes offset to a date."""
    return (horizon_start + timedelta(minutes=minutes)).date()
