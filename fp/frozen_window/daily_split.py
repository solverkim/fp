"""Daily Split — handle unmet production quantities.

After each production day, checks completed Work Orders for shortfalls.
Unmet quantities are split into child jobs with critical priority,
feeding back into the next solver run's demand pool.
"""

from __future__ import annotations

import frappe
from frappe import _


def process_daily_split() -> list[dict]:
	"""Daily scheduled job: split unmet quantities into child orders.

	For each FP-originated Work Order completed today with remaining qty,
	creates a child job in the latest Pre Plan snapshot's demand pool
	so the next solver run picks it up.

	Returns:
		List of created child job dicts.
	"""
	target_date = frappe.utils.getdate(frappe.utils.today())
	work_orders = get_completed_work_orders_with_shortfall(target_date)

	if not work_orders:
		frappe.logger("fp").info(
			f"Daily Split: no shortfall Work Orders for {target_date}"
		)
		return []

	created_children: list[dict] = []

	for wo in work_orders:
		open_qty = wo.qty - (wo.produced_qty or 0)
		if open_qty <= 0:
			continue

		child = create_child_job(
			parent_job_id=wo.custom_fp_snapshot_job or wo.name,
			remaining_qty=open_qty,
			original_due_date=wo.expected_delivery_date,
		)

		snapshot_job_name = add_to_demand_pool(child, wo.production_item)
		if snapshot_job_name:
			child["snapshot_job_name"] = snapshot_job_name
			created_children.append(child)

	if created_children:
		frappe.db.commit()
		frappe.logger("fp").info(
			f"Daily Split: created {len(created_children)} child jobs for {target_date}"
		)

	return created_children


def get_completed_work_orders_with_shortfall(target_date) -> list:
	"""Get FP-originated Work Orders that completed with unmet quantities.

	Filters Work Orders where:
	- custom_fp_snapshot_job is set (originated from FP)
	- Status indicates production activity occurred
	- produced_qty < qty (shortfall exists)

	Args:
		target_date: Date to check for completed work.

	Returns:
		List of Work Order docs with shortfall.
	"""
	wo_names = frappe.get_all(
		"Work Order",
		filters={
			"custom_fp_snapshot_job": ["is", "set"],
			"status": ["in", ["Completed", "Stopped"]],
			"modified": [">=", target_date],
			"docstatus": 1,
		},
		fields=[
			"name", "production_item", "qty", "produced_qty",
			"expected_delivery_date", "custom_fp_snapshot_job",
		],
	)

	return [
		wo for wo in wo_names
		if (wo.qty - (wo.produced_qty or 0)) > 0
	]


def create_child_job(
	parent_job_id: str,
	remaining_qty: float,
	original_due_date,
) -> dict:
	"""Create a child job for unmet quantity.

	Args:
		parent_job_id: Original job identifier (snapshot job name or W/O name).
		remaining_qty: Quantity not produced.
		original_due_date: Original due date (child inherits with critical priority).

	Returns:
		Dict representing the child job.
	"""
	return {
		"job_id": f"{parent_job_id}-SPLIT",
		"parent_job_id": parent_job_id,
		"qty": remaining_qty,
		"due_date": original_due_date,
		"priority": "critical",
	}


def add_to_demand_pool(child_job: dict, item_code: str) -> str | None:
	"""Add child job to the latest Pre Plan snapshot for the next solver run.

	Creates a new FP Snapshot Job row in the most recent Pre Plan snapshot
	so the solver includes the shortfall in its next optimization.

	Args:
		child_job: Dict from create_child_job().
		item_code: Production item code.

	Returns:
		Name of the created snapshot job row, or None if no Pre Plan exists.
	"""
	snapshot_name = frappe.db.get_value(
		"FP Planning Snapshot",
		filters={"status": "Pre Plan"},
		fieldname="name",
		order_by="creation desc",
	)

	if not snapshot_name:
		frappe.log_error(
			title="FP Daily Split — No Pre Plan",
			message=(
				f"No Pre Plan snapshot found to absorb child job "
				f"{child_job['job_id']}. Manual intervention required."
			),
		)
		return None

	snapshot = frappe.get_doc("FP Planning Snapshot", snapshot_name)

	row = snapshot.append("jobs", {
		"job_id": child_job["job_id"],
		"item_code": item_code,
		"qty": child_job["qty"],
		"workstation": "",
		"operation": "",
		"operation_sequence": 0,
		"planned_start": frappe.utils.now_datetime(),
		"planned_end": frappe.utils.now_datetime(),
		"due_date": child_job["due_date"],
		"source_demand_id": child_job["parent_job_id"],
		"is_frozen": 0,
	})

	snapshot.save(ignore_permissions=True)

	return row.name
