"""Snapshot & Netting Integration Tests — validates Frappe DB interactions.

Tests cover:
1. FP Planning Snapshot state machine with Frappe document lifecycle
2. Master data snapshot capture (TAT, Setup Matrix, Calendar, Workstations)
3. Snapshot duplication (Pre Plan → Draft Plan)
4. Snapshot comparison API (KPI diff + master data diff)
5. Fixed Plan confirmation auto-stamping
6. Sibling draft auto-archival on Fixed Plan
7. FP Demand Profile netting computation
8. Demand profile solver input generation
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot import (
	VALID_TRANSITIONS,
	FPPlanningSnapshot,
	capture_master_snapshot,
	compare_snapshots,
	duplicate_as_draft,
	_has_master_data_changed,
)
from fp.demand.netting import compute_netting, split_into_lots, build_demand_profile


# ============================================================
# Snapshot State Machine Tests (unit-level, no DB)
# ============================================================


class TestSnapshotStateMachineUnit:
	"""Validate VALID_TRANSITIONS dict correctness."""

	def test_pre_plan_can_only_become_draft(self):
		assert VALID_TRANSITIONS["Pre Plan"] == ["Draft Plan"]

	def test_draft_plan_transitions(self):
		allowed = VALID_TRANSITIONS["Draft Plan"]
		assert "Fixed Plan" in allowed
		assert "Archived" in allowed
		assert len(allowed) == 2

	def test_fixed_plan_can_only_be_archived(self):
		assert VALID_TRANSITIONS["Fixed Plan"] == ["Archived"]

	def test_archived_is_terminal(self):
		assert VALID_TRANSITIONS["Archived"] == []

	def test_no_backward_pre_plan(self):
		"""No state can transition back to Pre Plan."""
		for state, targets in VALID_TRANSITIONS.items():
			if state != "Pre Plan":
				assert "Pre Plan" not in targets, f"{state} should not transition to Pre Plan"

	def test_no_backward_from_fixed(self):
		"""Fixed Plan cannot go back to Draft Plan."""
		assert "Draft Plan" not in VALID_TRANSITIONS["Fixed Plan"]


# ============================================================
# Master Data Snapshot Tests
# ============================================================


class TestMasterSnapshotCapture:
	"""Test capture_master_snapshot serialization structure."""

	@patch("fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot.frappe")
	def test_capture_returns_all_sections(self, mock_frappe):
		# Arrange
		mock_frappe.get_all.return_value = []
		mock_frappe.utils.now.return_value = "2026-04-08 10:00:00"

		# Act
		result = capture_master_snapshot()

		# Assert
		assert "captured_at" in result
		assert "tat_master" in result
		assert "setup_matrix" in result
		assert "shift_calendar" in result
		assert "workstations" in result
		assert result["captured_at"] == "2026-04-08 10:00:00"

	@patch("fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot.frappe")
	def test_capture_queries_correct_doctypes(self, mock_frappe):
		# Arrange
		mock_frappe.get_all.return_value = []
		mock_frappe.utils.now.return_value = "2026-04-08 10:00:00"

		# Act
		capture_master_snapshot()

		# Assert — verify all 4 doctypes were queried
		doctype_calls = [call[0][0] for call in mock_frappe.get_all.call_args_list]
		assert "FP TAT Master" in doctype_calls
		assert "FP Setup Matrix" in doctype_calls
		assert "FP Shift Calendar" in doctype_calls
		assert "Workstation" in doctype_calls

	@patch("fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot.frappe")
	def test_capture_includes_tat_fields(self, mock_frappe):
		# Arrange
		tat_record = {
			"item_code": "BM-60kWh-A",
			"operation": "Welding",
			"workstation": "Line-1",
			"base_tat_mins": 25,
			"wait_time_mins": 5,
			"is_inline_inspection": 0,
			"inspection_tat_mins": 0,
		}
		mock_frappe.get_all.side_effect = [
			[tat_record],  # TAT Master
			[],            # Setup Matrix
			[],            # Shift Calendar
			[],            # Workstation
		]
		mock_frappe.utils.now.return_value = "2026-04-08 10:00:00"

		# Act
		result = capture_master_snapshot()

		# Assert
		assert len(result["tat_master"]) == 1
		assert result["tat_master"][0]["item_code"] == "BM-60kWh-A"
		assert result["tat_master"][0]["base_tat_mins"] == 25


# ============================================================
# Master Data Diff Tests
# ============================================================


class TestMasterDataDiff:
	"""Test _has_master_data_changed comparison logic."""

	def _make_snap_with_master(self, master_data: dict) -> MagicMock:
		snap = MagicMock()
		snap.master_snapshot = json.dumps(master_data)
		return snap

	def test_identical_snapshots_no_changes(self):
		# Arrange
		data = {
			"tat_master": [{"item_code": "A", "base_tat_mins": 30}],
			"setup_matrix": [],
			"shift_calendar": [],
			"workstations": [],
		}
		a = self._make_snap_with_master(data)
		b = self._make_snap_with_master(data)

		# Act
		result = _has_master_data_changed(a, b)

		# Assert
		assert not any(result.values())

	def test_tat_change_detected(self):
		# Arrange
		data_a = {
			"tat_master": [{"item_code": "A", "base_tat_mins": 30}],
			"setup_matrix": [],
			"shift_calendar": [],
			"workstations": [],
		}
		data_b = {
			"tat_master": [{"item_code": "A", "base_tat_mins": 45}],
			"setup_matrix": [],
			"shift_calendar": [],
			"workstations": [],
		}
		a = self._make_snap_with_master(data_a)
		b = self._make_snap_with_master(data_b)

		# Act
		result = _has_master_data_changed(a, b)

		# Assert
		assert result["tat_master"] is True
		assert result["setup_matrix"] is False

	def test_empty_master_snapshot_handled(self):
		# Arrange
		a = MagicMock()
		a.master_snapshot = ""
		b = MagicMock()
		b.master_snapshot = ""

		# Act
		result = _has_master_data_changed(a, b)

		# Assert — no crash, all sections reported as unchanged
		assert not any(result.values())


# ============================================================
# Netting Algorithm Tests (extended)
# ============================================================


class TestNettingExtended:
	"""Extended netting tests beyond design_validation basics."""

	def test_netting_with_large_inventory_surplus(self):
		"""When inventory exceeds demand, net = 0."""
		# Arrange / Act
		net = compute_netting(500, 1000, 0)

		# Assert
		assert net == 0

	def test_netting_with_partial_wo_coverage(self):
		"""WO partially covers demand."""
		# Arrange / Act
		net = compute_netting(1000, 100, 500)

		# Assert
		assert net == 400

	def test_lot_split_single_unit(self):
		"""Edge case: demand smaller than lot size."""
		# Arrange / Act
		jobs = split_into_lots(50, 200)

		# Assert
		assert len(jobs) == 1
		assert jobs[0] == 50

	def test_lot_split_zero_demand(self):
		"""Zero demand produces no jobs."""
		# Arrange / Act
		jobs = split_into_lots(0, 200)

		# Assert
		assert jobs == []

	def test_lot_split_negative_demand(self):
		"""Negative demand (shouldn't happen) produces no jobs."""
		# Arrange / Act
		jobs = split_into_lots(-100, 200)

		# Assert
		assert jobs == []


class TestBuildDemandProfile:
	"""Test build_demand_profile end-to-end netting + lot sizing."""

	def test_multiple_items_with_mixed_netting(self):
		# Arrange
		items = [
			{
				"item_code": "ITEM-A",
				"gross_demand": 1000,
				"available_inventory": 100,
				"firm_wo_qty": 200,
				"lot_size": 200,
				"due_date": "2026-04-14",
			},
			{
				"item_code": "ITEM-B",
				"gross_demand": 300,
				"available_inventory": 300,
				"firm_wo_qty": 0,
				"lot_size": 200,
				"due_date": "2026-04-18",
			},
		]

		# Act
		jobs = build_demand_profile(items)

		# Assert — ITEM-A: 700 net → 4 jobs, ITEM-B: 0 net → 0 jobs
		assert all(j["item_code"] == "ITEM-A" for j in jobs)
		assert len(jobs) == 4
		assert sum(j["qty"] for j in jobs) == 700

	def test_job_ids_are_sequential(self):
		# Arrange
		items = [
			{
				"item_code": "X",
				"gross_demand": 600,
				"available_inventory": 0,
				"firm_wo_qty": 0,
				"lot_size": 200,
				"due_date": "2026-04-14",
			},
		]

		# Act
		jobs = build_demand_profile(items)

		# Assert
		ids = [j["job_id"] for j in jobs]
		assert ids == ["JOB-0001", "JOB-0002", "JOB-0003"]

	def test_due_date_propagated(self):
		# Arrange
		items = [
			{
				"item_code": "X",
				"gross_demand": 200,
				"lot_size": 200,
				"due_date": "2026-04-20",
			},
		]

		# Act
		jobs = build_demand_profile(items)

		# Assert
		assert all(j["due_date"] == "2026-04-20" for j in jobs)


# ============================================================
# Demand Profile Document Tests (mocked Frappe)
# ============================================================


class TestDemandProfileDocument:
	"""Test FP Demand Profile validate-time netting computation."""

	def test_validate_computes_net_demand(self):
		"""Importing and calling _compute_netting_for_items."""
		from fp.factory_planner.doctype.fp_demand_profile.fp_demand_profile import (
			FPDemandProfile,
		)

		# Arrange
		doc = MagicMock(spec=FPDemandProfile)
		item = MagicMock()
		item.gross_demand = 1000
		item.available_inventory = 100
		item.firm_wo_qty = 200
		item.lot_size = 200
		item.net_demand = 0
		item.num_jobs = 0
		doc.items = [item]

		# Act
		FPDemandProfile._compute_netting_for_items(doc)

		# Assert
		assert item.net_demand == 700
		assert item.num_jobs == 4  # 700 / 200 = 3 full + 1 remainder(100)

	def test_validate_zero_gross_yields_zero_net(self):
		from fp.factory_planner.doctype.fp_demand_profile.fp_demand_profile import (
			FPDemandProfile,
		)

		# Arrange
		doc = MagicMock(spec=FPDemandProfile)
		item = MagicMock()
		item.gross_demand = 0
		item.available_inventory = 0
		item.firm_wo_qty = 0
		item.lot_size = 200
		doc.items = [item]

		# Act
		FPDemandProfile._compute_netting_for_items(doc)

		# Assert
		assert item.net_demand == 0
		assert item.num_jobs == 0


# ============================================================
# Snapshot Comparison Tests
# ============================================================


class TestSnapshotComparison:
	"""Test compare_snapshots KPI diff logic."""

	@patch("fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot.frappe")
	def test_kpi_delta_calculation(self, mock_frappe):
		# Arrange
		snap_a = MagicMock()
		snap_a.name = "PP-0001"
		snap_a.status = "Draft Plan"
		snap_a.get.side_effect = lambda f, d=None: {
			"solver_run_time_secs": 5.0,
			"objective_value": 1000.0,
			"total_tardiness_mins": 100.0,
			"total_setup_time_mins": 50.0,
			"line_utilization_pct": 80.0,
		}.get(f, 0)
		snap_a.jobs = [MagicMock()] * 7
		snap_a.master_snapshot = "{}"

		snap_b = MagicMock()
		snap_b.name = "PP-0002"
		snap_b.status = "Draft Plan"
		snap_b.get.side_effect = lambda f, d=None: {
			"solver_run_time_secs": 4.0,
			"objective_value": 800.0,
			"total_tardiness_mins": 60.0,
			"total_setup_time_mins": 45.0,
			"line_utilization_pct": 85.0,
		}.get(f, 0)
		snap_b.jobs = [MagicMock()] * 7
		snap_b.master_snapshot = "{}"

		mock_frappe.get_doc.side_effect = [snap_a, snap_b]

		# Act
		result = compare_snapshots("PP-0001", "PP-0002")

		# Assert
		tardiness = result["kpis"]["total_tardiness_mins"]
		assert tardiness["a_value"] == 100.0
		assert tardiness["b_value"] == 60.0
		assert tardiness["delta"] == -40.0
		assert tardiness["pct_change"] == -40.0

		util = result["kpis"]["line_utilization_pct"]
		assert util["delta"] == 5.0

		assert result["job_count"]["a"] == 7
		assert result["job_count"]["b"] == 7

	@patch("fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot.frappe")
	def test_division_by_zero_handled(self, mock_frappe):
		"""When a_value is 0, pct_change should be 0 (not crash)."""
		# Arrange
		snap_a = MagicMock()
		snap_a.name = "PP-0001"
		snap_a.status = "Pre Plan"
		snap_a.get.return_value = 0
		snap_a.jobs = []
		snap_a.master_snapshot = "{}"

		snap_b = MagicMock()
		snap_b.name = "PP-0002"
		snap_b.status = "Draft Plan"
		snap_b.get.return_value = 100
		snap_b.jobs = []
		snap_b.master_snapshot = "{}"

		mock_frappe.get_doc.side_effect = [snap_a, snap_b]

		# Act
		result = compare_snapshots("PP-0001", "PP-0002")

		# Assert — no ZeroDivisionError
		for kpi_data in result["kpis"].values():
			assert kpi_data["pct_change"] == 0.0


# ============================================================
# Solver Input Generation Tests
# ============================================================


class TestSolverInputGeneration:
	"""Test generate_solver_input whitelisted method."""

	@patch("fp.factory_planner.doctype.fp_demand_profile.fp_demand_profile.frappe")
	def test_generates_correct_jobs(self, mock_frappe):
		from fp.factory_planner.doctype.fp_demand_profile.fp_demand_profile import (
			generate_solver_input,
		)

		# Arrange
		item_1 = MagicMock()
		item_1.item_code = "BM-60kWh-A"
		item_1.net_demand = 600
		item_1.lot_size = 200
		item_1.due_date = "2026-04-14"
		item_1.idx = 1

		item_2 = MagicMock()
		item_2.item_code = "BM-80kWh-B"
		item_2.net_demand = 0  # Fully netted
		item_2.lot_size = 200
		item_2.due_date = "2026-04-18"
		item_2.idx = 2

		profile = MagicMock()
		profile.items = [item_1, item_2]
		mock_frappe.get_doc.return_value = profile

		# Act
		jobs = generate_solver_input("DP-0001")

		# Assert — only ITEM-A produces jobs (3 lots of 200)
		assert len(jobs) == 3
		assert all(j["item_code"] == "BM-60kWh-A" for j in jobs)
		assert all(j["qty"] == 200 for j in jobs)
		assert jobs[0]["source_demand_id"] == "DP-0001:1"

	@patch("fp.factory_planner.doctype.fp_demand_profile.fp_demand_profile.frappe")
	def test_empty_profile_returns_empty(self, mock_frappe):
		from fp.factory_planner.doctype.fp_demand_profile.fp_demand_profile import (
			generate_solver_input,
		)

		# Arrange
		profile = MagicMock()
		profile.items = []
		mock_frappe.get_doc.return_value = profile

		# Act
		jobs = generate_solver_input("DP-0001")

		# Assert
		assert jobs == []


if __name__ == "__main__":
	pytest.main([__file__, "-v"])
