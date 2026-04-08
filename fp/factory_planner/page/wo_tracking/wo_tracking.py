"""Backend API for Work Order Tracking page."""

from typing import Any

import frappe
from frappe import _


@frappe.whitelist()
def get_fixed_plan_jobs(snapshot_name: str) -> list[dict[str, Any]]:
	"""Get all jobs from a Fixed Plan snapshot with W/O release status.

	Returns list of jobs with work_order link and release state.
	"""
	snapshot = frappe.get_doc("FP Planning Snapshot", snapshot_name)

	if snapshot.status != "Fixed Plan":
		frappe.throw(
			_("Only Fixed Plan snapshots can be tracked. Current status: {0}").format(
				snapshot.status
			)
		)

	jobs = []
	for job in snapshot.jobs:
		wo_status = None
		wo_progress = 0
		if job.work_order:
			wo_doc = frappe.db.get_value(
				"Work Order",
				job.work_order,
				["status", "produced_qty", "qty"],
				as_dict=True,
			)
			if wo_doc:
				wo_status = wo_doc.status
				wo_progress = (
					round(float(wo_doc.produced_qty or 0) / float(wo_doc.qty) * 100, 1)
					if float(wo_doc.qty) > 0
					else 0
				)

		jobs.append({
			"job_id": job.job_id,
			"item_code": job.item_code,
			"qty": job.qty,
			"lot_number": job.lot_number,
			"workstation": job.workstation,
			"operation": job.operation,
			"planned_start": str(job.planned_start),
			"planned_end": str(job.planned_end),
			"due_date": str(job.due_date),
			"work_order": job.work_order,
			"wo_status": wo_status,
			"wo_progress": wo_progress,
			"source_demand_id": job.source_demand_id,
		})

	return jobs


@frappe.whitelist()
def get_order_genealogy(snapshot_name: str, source_demand_id: str) -> dict[str, Any]:
	"""Get order split genealogy for a given source demand.

	Traces all jobs descended from the same source demand to show
	how a customer order was split across lots and operations.
	"""
	snapshot = frappe.get_doc("FP Planning Snapshot", snapshot_name)

	related_jobs = [
		{
			"job_id": j.job_id,
			"item_code": j.item_code,
			"qty": j.qty,
			"lot_number": j.lot_number,
			"workstation": j.workstation,
			"operation": j.operation,
			"operation_sequence": j.operation_sequence,
			"planned_start": str(j.planned_start),
			"planned_end": str(j.planned_end),
			"work_order": j.work_order,
		}
		for j in snapshot.jobs
		if j.source_demand_id == source_demand_id
	]

	# Group by lot number
	lots: dict[int, list] = {}
	for job in related_jobs:
		lot = job.get("lot_number") or 0
		lots.setdefault(lot, []).append(job)

	# Sort jobs within each lot by operation sequence
	for lot_jobs in lots.values():
		lot_jobs.sort(key=lambda j: j.get("operation_sequence", 0))

	return {
		"source_demand_id": source_demand_id,
		"total_jobs": len(related_jobs),
		"lots": lots,
	}
