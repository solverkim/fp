"""FP Planning Snapshot — versioned production plans with state machine.

Workflow: Pre Plan → Draft Plan → Fixed Plan → Archived
- Pre Plan: auto-captures master data snapshot (TAT, Setup Matrix, Calendar)
- Draft Plan: created by duplicating a Pre Plan, inherits master snapshot
- Fixed Plan: locks the plan; sibling Drafts are auto-archived
- Archived: terminal state
"""

import json
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document


VALID_TRANSITIONS: dict[str, list[str]] = {
	"Pre Plan": ["Draft Plan"],
	"Draft Plan": ["Fixed Plan", "Archived"],
	"Fixed Plan": ["Archived"],
	"Archived": [],
}


class FPPlanningSnapshot(Document):
	def validate(self) -> None:
		self.validate_status_transition()

	def before_insert(self) -> None:
		if self.status == "Pre Plan" and not self.master_snapshot:
			self.master_snapshot = json.dumps(
				capture_master_snapshot(), ensure_ascii=False
			)

	def on_update(self) -> None:
		if self.status == "Fixed Plan":
			self._stamp_confirmation()
			self.archive_sibling_drafts()

	def _stamp_confirmation(self) -> None:
		"""Auto-set confirmed_by_user and confirmed_at on Fixed Plan transition."""
		if not self.confirmed_at:
			frappe.db.set_value(
				"FP Planning Snapshot",
				self.name,
				{
					"confirmed_by_user": frappe.session.user,
					"confirmed_at": frappe.utils.now_datetime(),
				},
				update_modified=False,
			)

	def validate_status_transition(self) -> None:
		if self.is_new():
			return

		old_status = self.db_get("status")
		if old_status == self.status:
			return

		allowed = VALID_TRANSITIONS.get(old_status, [])
		if self.status not in allowed:
			frappe.throw(
				_("Cannot transition from {0} to {1}. Allowed: {2}").format(
					old_status, self.status, ", ".join(allowed) or "None"
				)
			)

	def archive_sibling_drafts(self) -> None:
		if not self.parent_snapshot:
			return

		siblings = frappe.get_all(
			"FP Planning Snapshot",
			filters={
				"parent_snapshot": self.parent_snapshot,
				"status": "Draft Plan",
				"name": ["!=", self.name],
			},
			pluck="name",
		)
		for name in siblings:
			frappe.db.set_value("FP Planning Snapshot", name, "status", "Archived")


def capture_master_snapshot() -> dict[str, Any]:
	"""Serialize current master data (TAT, Setup Matrix, Calendar) to a dict.

	Called automatically when a Pre Plan snapshot is created.
	Returns a dict ready for JSON serialization into master_snapshot field.
	"""
	tat_records = frappe.get_all(
		"FP TAT Master",
		fields=[
			"item_code", "operation", "workstation",
			"base_tat_mins", "wait_time_mins",
			"is_inline_inspection", "inspection_tat_mins",
		],
	)

	setup_matrix_records = frappe.get_all(
		"FP Setup Matrix",
		fields=[
			"workstation", "from_setup_group", "to_setup_group",
			"setup_time_mins", "is_transition_allowed",
		],
	)

	calendar_records = frappe.get_all(
		"FP Shift Calendar",
		fields=[
			"workstation", "date", "shift_type",
			"start_time", "end_time", "break_duration_mins",
			"is_holiday", "available_capacity_mins",
		],
	)

	workstation_records = frappe.get_all(
		"Workstation",
		fields=[
			"name", "workstation_name", "production_capacity",
			"status",
		],
	)

	return {
		"captured_at": frappe.utils.now(),
		"tat_master": tat_records,
		"setup_matrix": setup_matrix_records,
		"shift_calendar": calendar_records,
		"workstations": workstation_records,
	}


@frappe.whitelist()
def duplicate_as_draft(source_name: str) -> str:
	"""Duplicate a Pre Plan snapshot as a new Draft Plan.

	Args:
		source_name: Name of the source FP Planning Snapshot (must be Pre Plan).

	Returns:
		Name of the newly created Draft Plan snapshot.
	"""
	source = frappe.get_doc("FP Planning Snapshot", source_name)

	if source.status != "Pre Plan":
		frappe.throw(
			_("Only Pre Plan snapshots can be duplicated. Current status: {0}").format(
				source.status
			)
		)

	draft = frappe.new_doc("FP Planning Snapshot")
	draft.snapshot_name = f"{source.snapshot_name} - Draft"
	draft.status = "Draft Plan"
	draft.planning_horizon_start = source.planning_horizon_start
	draft.planning_horizon_end = source.planning_horizon_end
	draft.parent_snapshot = source.name
	draft.master_snapshot = source.master_snapshot
	draft.created_by_user = frappe.session.user

	for job in source.jobs:
		draft.append("jobs", {
			"job_id": job.job_id,
			"item_code": job.item_code,
			"qty": job.qty,
			"lot_number": job.lot_number,
			"workstation": job.workstation,
			"operation": job.operation,
			"operation_sequence": job.operation_sequence,
			"planned_start": job.planned_start,
			"planned_end": job.planned_end,
			"setup_time_mins": job.setup_time_mins,
			"due_date": job.due_date,
			"tardiness_mins": job.tardiness_mins,
			"source_demand_id": job.source_demand_id,
			"is_frozen": 0,
			"work_order": None,
		})

	draft.insert()
	return draft.name


@frappe.whitelist()
def compare_snapshots(snapshot_a: str, snapshot_b: str) -> dict[str, Any]:
	"""Compare KPIs between two planning snapshots.

	Args:
		snapshot_a: Name of first snapshot.
		snapshot_b: Name of second snapshot.

	Returns:
		Dict with KPI differences (a_value, b_value, delta, pct_change).
	"""
	a = frappe.get_doc("FP Planning Snapshot", snapshot_a)
	b = frappe.get_doc("FP Planning Snapshot", snapshot_b)

	kpi_fields = [
		"solver_run_time_secs",
		"objective_value",
		"total_tardiness_mins",
		"total_setup_time_mins",
		"line_utilization_pct",
	]

	comparison: dict[str, Any] = {
		"snapshot_a": {"name": a.name, "status": a.status},
		"snapshot_b": {"name": b.name, "status": b.status},
		"kpis": {},
	}

	for field in kpi_fields:
		a_val = float(a.get(field) or 0)
		b_val = float(b.get(field) or 0)
		delta = b_val - a_val
		pct = (delta / a_val * 100) if a_val != 0 else 0.0

		comparison["kpis"][field] = {
			"a_value": a_val,
			"b_value": b_val,
			"delta": round(delta, 2),
			"pct_change": round(pct, 2),
		}

	# Job count comparison
	comparison["job_count"] = {
		"a": len(a.jobs),
		"b": len(b.jobs),
	}

	# Master data diff summary
	comparison["master_data_changed"] = _has_master_data_changed(a, b)

	return comparison


def _has_master_data_changed(
	snap_a: "FPPlanningSnapshot", snap_b: "FPPlanningSnapshot"
) -> dict[str, bool]:
	"""Check which master data sections differ between two snapshots.

	Returns dict like {"tat_master": True, "setup_matrix": False, ...}.
	"""
	result = {}
	a_data = json.loads(snap_a.master_snapshot or "{}")
	b_data = json.loads(snap_b.master_snapshot or "{}")

	for section in ("tat_master", "setup_matrix", "shift_calendar", "workstations"):
		a_section = a_data.get(section, [])
		b_section = b_data.get(section, [])
		result[section] = a_section != b_section

	return result
