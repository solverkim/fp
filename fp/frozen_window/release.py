"""Frozen Window — D+2 Work Order release logic.

Daily scheduled job that converts Fixed Plan snapshot jobs into ERPNext Work Orders.
Jobs within the D+2 frozen window are marked as frozen and cannot be rescheduled.
"""

from __future__ import annotations

from datetime import date, timedelta

import frappe
from frappe import _


def release_frozen_window_orders() -> list[str]:
	"""Daily scheduled job: release D+2 frozen jobs as Work Orders.

	Finds the active Fixed Plan snapshot, filters jobs whose planned_start
	falls on D+2 (today + 2 days), marks them frozen, and creates
	corresponding ERPNext Work Orders.

	Returns:
		List of created Work Order names.
	"""
	target_date = frappe.utils.getdate(frappe.utils.today()) + timedelta(days=2)
	snapshot = get_active_fixed_plan()

	if not snapshot:
		frappe.log_error(
			title="FP Frozen Window",
			message="No active Fixed Plan snapshot found. Skipping D+2 release.",
		)
		return []

	frozen_jobs = get_frozen_jobs(snapshot.name, target_date)

	if not frozen_jobs:
		frappe.logger("fp").info(
			f"No jobs to freeze for {target_date} in snapshot {snapshot.name}"
		)
		return []

	created_work_orders: list[str] = []

	for job in frozen_jobs:
		if job.is_frozen or job.work_order:
			continue

		wo_name = create_work_order_from_job(job)
		if wo_name:
			mark_job_frozen(snapshot.name, job.name, wo_name)
			created_work_orders.append(wo_name)

	if created_work_orders:
		frappe.db.commit()
		frappe.logger("fp").info(
			f"Frozen Window: created {len(created_work_orders)} Work Orders "
			f"for {target_date} from snapshot {snapshot.name}"
		)

	return created_work_orders


def get_active_fixed_plan() -> object | None:
	"""Get the most recent Fixed Plan snapshot.

	Returns:
		FP Planning Snapshot doc or None if no Fixed Plan exists.
	"""
	name = frappe.db.get_value(
		"FP Planning Snapshot",
		filters={"status": "Fixed Plan"},
		fieldname="name",
		order_by="confirmed_at desc, creation desc",
	)

	if not name:
		return None

	return frappe.get_doc("FP Planning Snapshot", name)


def get_frozen_jobs(snapshot_name: str, target_date: date) -> list:
	"""Get jobs from a Fixed Plan snapshot that should be frozen for target_date.

	Filters snapshot jobs where the planned_start date matches target_date
	and the job is not already frozen or linked to a Work Order.

	Args:
		snapshot_name: FP Planning Snapshot name.
		target_date: The D+2 date to freeze.

	Returns:
		List of FP Snapshot Job child docs matching the target date.
	"""
	snapshot = frappe.get_doc("FP Planning Snapshot", snapshot_name)
	target = frappe.utils.getdate(target_date)

	return [
		job for job in snapshot.jobs
		if _extract_date(job.planned_start) == target
		if not job.is_frozen and not job.work_order
	]


def _extract_date(dt_value) -> date:
	"""Extract a date from a datetime or date value."""
	if hasattr(dt_value, "date"):
		return dt_value.date()
	return frappe.utils.getdate(dt_value)


def create_work_order_from_job(job) -> str | None:
	"""Create an ERPNext Work Order from a snapshot job.

	Field mapping per design doc section 4.1:
		production_item  <- job.item_code
		qty              <- job.qty
		bom_no           <- Item's default BOM
		planned_start_date <- job.planned_start
		expected_delivery_date <- job.due_date
		custom_fp_snapshot_job <- job.name (reverse lookup)

	Args:
		job: FP Snapshot Job child doc row.

	Returns:
		Work Order name on success, None on failure.
	"""
	bom_no = get_default_bom(job.item_code)
	if not bom_no:
		frappe.log_error(
			title="FP Frozen Window — Missing BOM",
			message=f"No default BOM found for item {job.item_code}. "
			f"Skipping Work Order creation for job {job.job_id}.",
		)
		return None

	wo = frappe.new_doc("Work Order")
	wo.production_item = job.item_code
	wo.qty = job.qty
	wo.bom_no = bom_no
	wo.planned_start_date = job.planned_start
	wo.expected_delivery_date = job.due_date
	wo.custom_fp_snapshot_job = job.name
	wo.company = frappe.defaults.get_defaults().get("company")

	wo.insert()
	wo.submit()

	return wo.name


def get_default_bom(item_code: str) -> str | None:
	"""Get the default BOM for an item.

	Args:
		item_code: Item code to look up.

	Returns:
		Default BOM name or None.
	"""
	return frappe.db.get_value(
		"BOM",
		filters={"item": item_code, "is_default": 1, "is_active": 1},
		fieldname="name",
	)


def mark_job_frozen(snapshot_name: str, job_row_name: str, work_order: str) -> None:
	"""Mark a snapshot job as frozen and link the created Work Order.

	Args:
		snapshot_name: Parent FP Planning Snapshot name.
		job_row_name: Child table row name of the FP Snapshot Job.
		work_order: Created Work Order name.
	"""
	frappe.db.set_value(
		"FP Snapshot Job",
		job_row_name,
		{"is_frozen": 1, "work_order": work_order},
	)
