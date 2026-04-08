"""Solver Runner — Frappe-integrated async execution with realtime progress.

Provides ``run_solver`` for synchronous use and ``enqueue_solver`` for
background job execution via ``frappe.enqueue()``.  Progress updates are
pushed to the browser through Frappe Realtime (``frappe.publish_realtime``).
"""

from __future__ import annotations

import time
import traceback
from datetime import date

import frappe

from fp.solver.data_loader import (
    build_master_snapshot,
    load_solver_config_from_doctype,
    load_solver_inputs,
)
from fp.solver.engine import SolverResult, solve
from fp.solver.result_writer import write_snapshot


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

PROGRESS_EVENT = "fp_solver_progress"


def _publish_progress(
    stage: str,
    percent: int,
    message: str,
    snapshot_name: str = "",
) -> None:
    """Push a realtime progress event to the browser."""
    frappe.publish_realtime(
        PROGRESS_EVENT,
        {
            "stage": stage,
            "percent": min(percent, 100),
            "message": message,
            "snapshot_name": snapshot_name,
        },
        doctype="FP Planning Snapshot",
    )


# ---------------------------------------------------------------------------
# Synchronous runner
# ---------------------------------------------------------------------------


def run_solver(
    demand_jobs: list[dict],
    horizon_start: date,
    horizon_end: date,
    snapshot_name: str,
) -> dict:
    """Run the full solve pipeline synchronously.

    Args:
        demand_jobs: Output of ``build_demand_profile`` — list of job dicts.
        horizon_start: Planning horizon start date.
        horizon_end: Planning horizon end date.
        snapshot_name: Human-readable name for the resulting snapshot.

    Returns:
        dict with keys: snapshot_id, status, objective_value, runtime_secs,
        total_jobs, solver_used.
    """
    t0 = time.time()

    # Stage 1 — Load master data
    _publish_progress("load", 10, "Loading master data …", snapshot_name)
    jobs, workstations, setup_matrix, shift_capacity = load_solver_inputs(
        demand_jobs, horizon_start, horizon_end,
    )

    if not jobs:
        _publish_progress("error", 0, "No schedulable jobs found.", snapshot_name)
        return {
            "snapshot_id": None,
            "status": "ERROR",
            "message": "No schedulable jobs after filtering frozen jobs.",
        }

    # Stage 2 — Load solver config
    config = load_solver_config_from_doctype()
    _publish_progress(
        "solve",
        20,
        f"Solving {len(jobs)} jobs on {len(workstations)} workstations …",
        snapshot_name,
    )

    # Stage 3 — Run solver
    result = solve(
        jobs=jobs,
        workstations=workstations,
        setup_matrix=setup_matrix,
        shift_capacity=shift_capacity,
        config=config,
    )

    if result.status in ("INFEASIBLE", "ERROR"):
        _publish_progress("error", 0, f"Solver returned {result.status}", snapshot_name)
        return {
            "snapshot_id": None,
            "status": result.status,
            "message": f"Solver could not find a solution: {result.status}",
            "runtime_secs": result.runtime_secs,
        }

    _publish_progress("write", 80, "Writing planning snapshot …", snapshot_name)

    # Stage 4 — Build master snapshot for audit
    master_snapshot = build_master_snapshot(horizon_start, horizon_end)

    # Stage 5 — Persist results
    snapshot_id = write_snapshot(
        result=result,
        snapshot_name=snapshot_name,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        master_snapshot=master_snapshot,
    )

    total_time = round(time.time() - t0, 3)
    _publish_progress("done", 100, "Planning complete.", snapshot_name)

    return {
        "snapshot_id": snapshot_id,
        "status": result.status,
        "objective_value": result.objective_value,
        "runtime_secs": total_time,
        "solver_runtime_secs": result.runtime_secs,
        "total_jobs": len(jobs),
        "total_scheduled_ops": len(result.scheduled_jobs),
        "total_tardiness_mins": result.total_tardiness_mins,
        "total_setup_time_mins": result.total_setup_time_mins,
        "solver_used": result.solver_used,
    }


# ---------------------------------------------------------------------------
# Async (background job) runner
# ---------------------------------------------------------------------------


def enqueue_solver(
    demand_jobs: list[dict],
    horizon_start: str,
    horizon_end: str,
    snapshot_name: str,
    queue: str = "long",
    timeout: int = 600,
) -> str:
    """Enqueue a solver run as a Frappe background job.

    Args:
        demand_jobs: Output of ``build_demand_profile``.
        horizon_start: ISO date string (YYYY-MM-DD).
        horizon_end: ISO date string (YYYY-MM-DD).
        snapshot_name: Human-readable snapshot name.
        queue: RQ queue name (default "long").
        timeout: Job timeout in seconds (default 600).

    Returns:
        The RQ job ID for tracking.
    """
    job = frappe.enqueue(
        "fp.solver.runner._background_solve",
        queue=queue,
        timeout=timeout,
        is_async=True,
        demand_jobs=demand_jobs,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        snapshot_name=snapshot_name,
    )
    return job.id


def _background_solve(
    demand_jobs: list[dict],
    horizon_start: str,
    horizon_end: str,
    snapshot_name: str,
) -> None:
    """Background job entry point — wraps ``run_solver`` with error handling."""
    try:
        h_start = date.fromisoformat(horizon_start)
        h_end = date.fromisoformat(horizon_end)

        result = run_solver(demand_jobs, h_start, h_end, snapshot_name)

        _publish_progress(
            "done" if result.get("snapshot_id") else "error",
            100 if result.get("snapshot_id") else 0,
            f"Solver finished: {result.get('status')} — {result.get('total_jobs', 0)} jobs",
            snapshot_name,
        )

    except Exception:
        frappe.log_error(
            title=f"FP Solver Error: {snapshot_name}",
            message=traceback.format_exc(),
        )
        _publish_progress("error", 0, "Solver failed — check Error Log.", snapshot_name)


# ---------------------------------------------------------------------------
# Whitelisted API for frontend
# ---------------------------------------------------------------------------


@frappe.whitelist()
def run_planning(
    snapshot_name: str,
    horizon_start: str,
    horizon_end: str,
    demand_profile_name: str | None = None,
    async_mode: bool = True,
) -> dict:
    """Frappe whitelisted API to trigger a planning run.

    Can be called from the browser or via ``frappe.call``.

    Args:
        snapshot_name: Name for the resulting snapshot.
        horizon_start: ISO date string.
        horizon_end: ISO date string.
        demand_profile_name: Optional FP Demand Profile doctype name to load
            demand jobs from.  If not provided, caller must supply demand_jobs
            via the synchronous path.
        async_mode: If True (default), enqueue as background job.

    Returns:
        dict with job_id (async) or full result (sync).
    """
    demand_jobs = _load_demand_from_profile(demand_profile_name) if demand_profile_name else []

    if not demand_jobs:
        frappe.throw("No demand jobs found. Provide a valid Demand Profile.")

    if async_mode:
        job_id = enqueue_solver(
            demand_jobs=demand_jobs,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            snapshot_name=snapshot_name,
        )
        return {"job_id": job_id, "message": "Solver enqueued."}

    h_start = date.fromisoformat(horizon_start)
    h_end = date.fromisoformat(horizon_end)
    return run_solver(demand_jobs, h_start, h_end, snapshot_name)


def _load_demand_from_profile(profile_name: str) -> list[dict]:
    """Load demand jobs from an FP Demand Profile doctype."""
    from fp.demand.netting import build_demand_profile

    doc = frappe.get_doc("FP Demand Profile", profile_name)
    demand_items = []
    for row in doc.items:
        demand_items.append({
            "item_code": row.item_code,
            "gross_demand": row.gross_demand,
            "available_inventory": row.available_inventory or 0,
            "firm_wo_qty": row.firm_wo_qty or 0,
            "lot_size": row.lot_size or 200,
            "due_date": str(row.due_date),
        })

    return build_demand_profile(demand_items)
