"""Backend API for Gantt Tuning page — validate and apply job reschedules."""

from typing import Any

import frappe
from frappe import _


@frappe.whitelist()
def validate_reschedule(
	snapshot_name: str,
	job_id: str,
	new_workstation: str,
	new_start: str,
) -> dict[str, Any]:
	"""Validate a job reschedule and apply it if valid.

	Checks:
	1. Snapshot must be Draft Plan (not Fixed or Pre Plan).
	2. Job must not be frozen.
	3. New workstation must be compatible (setup group transition allowed).
	4. Recalculates setup time based on preceding job on target workstation.

	Returns:
		dict with 'valid' bool, 'violations' list, and 'new_setup_time' if valid.
	"""
	snapshot = frappe.get_doc("FP Planning Snapshot", snapshot_name)
	violations: list[str] = []

	if snapshot.status != "Draft Plan":
		violations.append(
			_("Only Draft Plan snapshots can be edited. Current status: {0}").format(
				snapshot.status
			)
		)
		return {"valid": False, "violations": violations}

	target_job = None
	target_idx = None
	for idx, job in enumerate(snapshot.jobs):
		if job.job_id == job_id:
			target_job = job
			target_idx = idx
			break

	if not target_job:
		violations.append(_("Job {0} not found in snapshot.").format(job_id))
		return {"valid": False, "violations": violations}

	if target_job.is_frozen:
		violations.append(_("Job {0} is frozen and cannot be moved.").format(job_id))
		return {"valid": False, "violations": violations}

	new_start_dt = frappe.utils.get_datetime(new_start)

	# Check frozen window — cannot schedule before frozen boundary
	frozen_boundary = _get_frozen_boundary(snapshot, new_workstation)
	if frozen_boundary and new_start_dt < frozen_boundary:
		violations.append(
			_("Cannot schedule before frozen boundary ({0}) on workstation {1}.").format(
				frappe.utils.format_datetime(frozen_boundary), new_workstation
			)
		)

	# Calculate new setup time based on preceding job
	new_setup_time = _calc_setup_time(
		snapshot, target_job, new_workstation, new_start_dt
	)

	# Check if the setup group transition is allowed
	transition_violation = _check_transition_allowed(
		snapshot, target_job, new_workstation, new_start_dt
	)
	if transition_violation:
		violations.append(transition_violation)

	# Compute new end time
	duration_mins = frappe.utils.time_diff_in_seconds(
		target_job.planned_end, target_job.planned_start
	) / 60
	# Adjust duration for setup time difference
	processing_mins = duration_mins - float(target_job.setup_time_mins or 0)
	new_duration_mins = processing_mins + new_setup_time
	new_end_dt = frappe.utils.add_to_date(new_start_dt, minutes=new_duration_mins)

	# Check shift capacity on new workstation
	capacity_ok = _check_shift_capacity(new_workstation, new_start_dt, new_end_dt)
	if not capacity_ok:
		violations.append(
			_("Insufficient shift capacity on {0} for the scheduled time window.").format(
				new_workstation
			)
		)

	if violations:
		return {"valid": False, "violations": violations}

	# Apply the reschedule
	snapshot.jobs[target_idx].workstation = new_workstation
	snapshot.jobs[target_idx].planned_start = new_start_dt
	snapshot.jobs[target_idx].planned_end = new_end_dt
	snapshot.jobs[target_idx].setup_time_mins = new_setup_time

	# Recalculate tardiness
	due_date_dt = frappe.utils.get_datetime(
		str(target_job.due_date) + " 23:59:59"
	)
	if new_end_dt > due_date_dt:
		tardiness = frappe.utils.time_diff_in_seconds(new_end_dt, due_date_dt) / 60
		snapshot.jobs[target_idx].tardiness_mins = round(tardiness, 1)
	else:
		snapshot.jobs[target_idx].tardiness_mins = 0

	snapshot.save()

	return {
		"valid": True,
		"violations": [],
		"new_setup_time": new_setup_time,
		"new_end": str(new_end_dt),
		"tardiness_mins": snapshot.jobs[target_idx].tardiness_mins,
	}


def _get_frozen_boundary(snapshot, workstation: str):
	"""Get the latest planned_end of frozen jobs on a workstation."""
	frozen_ends = [
		frappe.utils.get_datetime(j.planned_end)
		for j in snapshot.jobs
		if j.workstation == workstation and j.is_frozen
	]
	return max(frozen_ends) if frozen_ends else None


def _calc_setup_time(snapshot, target_job, new_workstation: str, new_start_dt):
	"""Calculate setup time based on preceding job's setup group."""
	# Find the job immediately before on the target workstation
	ws_jobs = sorted(
		[
			j
			for j in snapshot.jobs
			if j.workstation == new_workstation
			and j.job_id != target_job.job_id
			and frappe.utils.get_datetime(j.planned_end) <= new_start_dt
		],
		key=lambda j: frappe.utils.get_datetime(j.planned_end),
	)

	if not ws_jobs:
		return 0.0

	preceding_job = ws_jobs[-1]

	# Look up setup groups for both items
	from_group = _get_setup_group(preceding_job.item_code)
	to_group = _get_setup_group(target_job.item_code)

	if not from_group or not to_group:
		return float(target_job.setup_time_mins or 0)

	if from_group == to_group:
		return 0.0

	# Look up setup matrix
	matrix_entry = frappe.db.get_value(
		"FP Setup Matrix",
		{
			"workstation": new_workstation,
			"from_setup_group": from_group,
			"to_setup_group": to_group,
		},
		["setup_time_mins", "is_transition_allowed"],
		as_dict=True,
	)

	if matrix_entry:
		return float(matrix_entry.setup_time_mins or 0)

	# Fallback: keep original setup time
	return float(target_job.setup_time_mins or 0)


def _check_transition_allowed(snapshot, target_job, new_workstation: str, new_start_dt):
	"""Check if the setup group transition is allowed on this workstation."""
	ws_jobs = sorted(
		[
			j
			for j in snapshot.jobs
			if j.workstation == new_workstation
			and j.job_id != target_job.job_id
			and frappe.utils.get_datetime(j.planned_end) <= new_start_dt
		],
		key=lambda j: frappe.utils.get_datetime(j.planned_end),
	)

	if not ws_jobs:
		return None

	preceding_job = ws_jobs[-1]
	from_group = _get_setup_group(preceding_job.item_code)
	to_group = _get_setup_group(target_job.item_code)

	if not from_group or not to_group or from_group == to_group:
		return None

	matrix_entry = frappe.db.get_value(
		"FP Setup Matrix",
		{
			"workstation": new_workstation,
			"from_setup_group": from_group,
			"to_setup_group": to_group,
		},
		"is_transition_allowed",
	)

	if matrix_entry is not None and not matrix_entry:
		return _(
			"Transition from {0} to {1} is BLOCKED on {2}. "
			"Check Setup Matrix configuration."
		).format(from_group, to_group, new_workstation)

	return None


def _get_setup_group(item_code: str) -> str | None:
	"""Find the setup group that contains this item."""
	result = frappe.db.get_value(
		"FP Setup Group Item",
		{"item_code": item_code},
		"parent",
	)
	return result


def _check_shift_capacity(workstation: str, start_dt, end_dt) -> bool:
	"""Check if the workstation has shift coverage for the time window."""
	start_date = frappe.utils.getdate(start_dt)
	end_date = frappe.utils.getdate(end_dt)

	shifts = frappe.get_all(
		"FP Shift Calendar",
		filters={
			"workstation": workstation,
			"date": ["between", [str(start_date), str(end_date)]],
			"is_holiday": 0,
		},
		fields=["date", "start_time", "end_time", "available_capacity_mins"],
	)

	# If no shift records exist, allow (assume unconstrained)
	if not shifts:
		return True

	# Basic check: at least one shift covers the start date
	return any(str(s.date) == str(start_date) for s in shifts)
