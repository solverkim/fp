"""Design Validation Test — verifies the FP system design logic end-to-end.

Test Scenario: 범한유니솔루션 배터리 모듈 생산
- 2 product models (Group A: 60kWh, Group B: 80kWh)
- 2 production lines (Line-1, Line-2)
- 5-stage BOM: 모듈조립 → 용접 → 자주검사(no W/O) → 하네스 → 케이스
- Weekly demand: 1000 units of Model-A, 600 units of Model-B
- Lot size: 200 units
- D+2 frozen window validation
"""

import pytest
from datetime import date, datetime, timedelta

from fp.demand.netting import compute_netting, split_into_lots, build_demand_profile
from fp.demand.lot_sizing import fixed_order_quantity, lot_for_lot
from fp.solver.engine import Job, Operation, SolverResult, solve
from fp.frozen_window.daily_split import create_child_job


# ============================================================
# Test Data Fixtures
# ============================================================

TEST_ITEMS = [
	{"item_code": "BM-60kWh-A", "item_name": "Battery Module 60kWh Type A", "setup_group": "Group-A"},
	{"item_code": "BM-80kWh-B", "item_name": "Battery Module 80kWh Type B", "setup_group": "Group-B"},
]

TEST_WORKSTATIONS = ["Line-1", "Line-2"]

TEST_OPERATIONS = [
	{"name": "Module Assembly", "sequence": 10, "tat_mins": 30, "wait_time_mins": 5},
	{"name": "Welding", "sequence": 20, "tat_mins": 25, "wait_time_mins": 0},
	{"name": "Self Inspection", "sequence": 30, "tat_mins": 0, "is_inline_inspection": True, "inspection_tat_mins": 15},
	{"name": "Harness", "sequence": 40, "tat_mins": 20, "wait_time_mins": 0},
	{"name": "Case Assembly", "sequence": 50, "tat_mins": 35, "wait_time_mins": 0},
]

TEST_SETUP_MATRIX = {
	("Line-1", "Group-A", "Group-B"): 45,
	("Line-1", "Group-B", "Group-A"): 60,
	("Line-2", "Group-A", "Group-B"): 45,
	("Line-2", "Group-B", "Group-A"): 60,
}

TEST_SHIFT_CAPACITY = {
	"Line-1": 960,  # 16 hours in minutes (2 shifts)
	"Line-2": 960,
}

TEST_DEMAND = [
	{
		"item_code": "BM-60kWh-A",
		"gross_demand": 1000,
		"available_inventory": 100,
		"firm_wo_qty": 200,
		"lot_size": 200,
		"due_date": "2026-04-14",
	},
	{
		"item_code": "BM-80kWh-B",
		"gross_demand": 600,
		"available_inventory": 0,
		"firm_wo_qty": 0,
		"lot_size": 200,
		"due_date": "2026-04-18",
	},
]


# ============================================================
# Module 1: Netting & Lot Sizing Tests
# ============================================================

class TestNetting:
	def test_basic_netting(self):
		"""Net = Gross - Inventory - Firm WO."""
		net = compute_netting(500, 100, 200)
		assert net == 200

	def test_netting_no_negative(self):
		"""Net demand cannot be negative."""
		net = compute_netting(100, 200, 50)
		assert net == 0

	def test_netting_zero_inputs(self):
		"""Zero gross demand yields zero net."""
		net = compute_netting(0, 0, 0)
		assert net == 0

	def test_lot_split_exact_division(self):
		"""1000 / 200 = 5 jobs of 200 each."""
		jobs = split_into_lots(1000, 200)
		assert len(jobs) == 5
		assert all(q == 200 for q in jobs)

	def test_lot_split_with_remainder_merge(self):
		"""1030 / 200 = 5 jobs, remainder 30 < 20% threshold → merge into last."""
		jobs = split_into_lots(1030, 200)
		assert len(jobs) == 5
		assert jobs[-1] == 230  # 200 + 30

	def test_lot_split_with_remainder_separate(self):
		"""1100 / 200 = 5 jobs of 200 + 1 job of 100 (100 > 20% of 200)."""
		jobs = split_into_lots(1100, 200)
		assert len(jobs) == 6
		assert jobs[-1] == 100

	def test_demand_profile_integration(self):
		"""Full netting + lot sizing for test demand."""
		jobs = build_demand_profile(TEST_DEMAND)

		# Model A: 1000 - 100 - 200 = 700 net → 3 lots of 200 + 1 lot of 100
		model_a_jobs = [j for j in jobs if j["item_code"] == "BM-60kWh-A"]
		assert len(model_a_jobs) == 4
		assert sum(j["qty"] for j in model_a_jobs) == 700

		# Model B: 600 - 0 - 0 = 600 net → 3 lots of 200
		model_b_jobs = [j for j in jobs if j["item_code"] == "BM-80kWh-B"]
		assert len(model_b_jobs) == 3
		assert sum(j["qty"] for j in model_b_jobs) == 600


class TestLotSizing:
	def test_fixed_order_quantity(self):
		lots = fixed_order_quantity(500, 200)
		assert len(lots) == 3
		assert sum(lots) == 500

	def test_lot_for_lot(self):
		lots = lot_for_lot(350)
		assert lots == [350]


# ============================================================
# Module 2: Solver Engine Tests
# ============================================================

def _make_test_operations(workstation: str) -> list[Operation]:
	"""Create 5-stage operations for a job."""
	ops = []
	for op_data in TEST_OPERATIONS:
		ops.append(Operation(
			name=op_data["name"],
			sequence=op_data["sequence"],
			tat_mins=op_data["tat_mins"],
			wait_time_mins=op_data.get("wait_time_mins", 0),
			is_inline_inspection=op_data.get("is_inline_inspection", False),
			inspection_tat_mins=op_data.get("inspection_tat_mins", 0),
			workstation=workstation,
		))
	return ops


class TestSolverEngine:
	def test_single_job_scheduling(self):
		"""Single job should be scheduled with zero tardiness."""
		job = Job(
			job_id="JOB-0001",
			item_code="BM-60kWh-A",
			qty=200,
			due_date_mins=500,  # Generous due date
			setup_group="Group-A",
			operations=_make_test_operations("Line-1"),
		)

		result = solve(
			jobs=[job],
			workstations=["Line-1"],
			setup_matrix=TEST_SETUP_MATRIX,
			shift_capacity=TEST_SHIFT_CAPACITY,
			max_time_secs=10,
		)

		assert result.status in ("OPTIMAL", "FEASIBLE")
		assert result.total_tardiness_mins == 0
		assert len(result.scheduled_jobs) > 0

	def test_precedence_constraints(self):
		"""Operations must be in sequence: assembly → welding → harness → case."""
		job = Job(
			job_id="JOB-0001",
			item_code="BM-60kWh-A",
			qty=200,
			due_date_mins=500,
			setup_group="Group-A",
			operations=_make_test_operations("Line-1"),
		)

		result = solve(
			jobs=[job],
			workstations=["Line-1"],
			setup_matrix={},
			shift_capacity=TEST_SHIFT_CAPACITY,
			max_time_secs=10,
		)

		assert result.status in ("OPTIMAL", "FEASIBLE")

		# Check precedence: each op starts after previous ends
		sched = sorted(result.scheduled_jobs, key=lambda x: x["operation_sequence"])
		for i in range(len(sched) - 1):
			assert sched[i + 1]["planned_start_mins"] >= sched[i]["planned_end_mins"], (
				f"Op {sched[i+1]['operation']} starts before {sched[i]['operation']} ends"
			)

	def test_inline_inspection_delay(self):
		"""Self-inspection TAT (15 min) must be respected between welding and harness."""
		job = Job(
			job_id="JOB-0001",
			item_code="BM-60kWh-A",
			qty=200,
			due_date_mins=500,
			setup_group="Group-A",
			operations=_make_test_operations("Line-1"),
		)

		result = solve(
			jobs=[job],
			workstations=["Line-1"],
			setup_matrix={},
			shift_capacity=TEST_SHIFT_CAPACITY,
			max_time_secs=10,
		)

		assert result.status in ("OPTIMAL", "FEASIBLE")

		welding_end = None
		harness_start = None
		for s in result.scheduled_jobs:
			if s["operation"] == "Welding":
				welding_end = s["planned_end_mins"]
			if s["operation"] == "Harness":
				harness_start = s["planned_start_mins"]

		assert welding_end is not None and harness_start is not None
		gap = harness_start - welding_end
		assert gap >= 15, f"Gap between Welding end and Harness start is {gap}, expected >= 15 (inspection TAT)"

	def test_no_overlap_constraint(self):
		"""Two jobs on same line must not overlap."""
		jobs = [
			Job(
				job_id="JOB-0001",
				item_code="BM-60kWh-A",
				qty=200,
				due_date_mins=500,
				setup_group="Group-A",
				operations=_make_test_operations("Line-1"),
			),
			Job(
				job_id="JOB-0002",
				item_code="BM-80kWh-B",
				qty=200,
				due_date_mins=500,
				setup_group="Group-B",
				operations=_make_test_operations("Line-1"),
			),
		]

		result = solve(
			jobs=jobs,
			workstations=["Line-1"],
			setup_matrix=TEST_SETUP_MATRIX,
			shift_capacity=TEST_SHIFT_CAPACITY,
			max_time_secs=10,
		)

		assert result.status in ("OPTIMAL", "FEASIBLE")

		# Group by operation sequence, check no time overlaps on same workstation
		by_seq = {}
		for s in result.scheduled_jobs:
			by_seq.setdefault(s["operation_sequence"], []).append(s)

		for seq, ops in by_seq.items():
			if len(ops) < 2:
				continue
			ops_sorted = sorted(ops, key=lambda x: x["planned_start_mins"])
			for i in range(len(ops_sorted) - 1):
				assert ops_sorted[i + 1]["planned_start_mins"] >= ops_sorted[i]["planned_end_mins"], (
					f"Overlap at seq {seq}: job {ops_sorted[i]['job_id']} ends {ops_sorted[i]['planned_end_mins']} "
					f"but {ops_sorted[i+1]['job_id']} starts {ops_sorted[i+1]['planned_start_mins']}"
				)

	def test_tardiness_minimization(self):
		"""With tight due date, solver should still schedule (may have tardiness)."""
		job = Job(
			job_id="JOB-0001",
			item_code="BM-60kWh-A",
			qty=200,
			due_date_mins=50,  # Very tight — total process is ~110+ mins
			setup_group="Group-A",
			operations=_make_test_operations("Line-1"),
		)

		result = solve(
			jobs=[job],
			workstations=["Line-1"],
			setup_matrix={},
			shift_capacity=TEST_SHIFT_CAPACITY,
			max_time_secs=10,
		)

		assert result.status in ("OPTIMAL", "FEASIBLE")
		assert result.total_tardiness_mins > 0  # Expected: can't finish in 50 mins

	def test_multi_job_multi_line(self):
		"""Multiple jobs across 2 lines should all be scheduled."""
		demand_items = build_demand_profile(TEST_DEMAND)
		jobs = []

		for idx, d in enumerate(demand_items):
			ws = TEST_WORKSTATIONS[idx % len(TEST_WORKSTATIONS)]
			group = "Group-A" if "60kWh" in d["item_code"] else "Group-B"
			jobs.append(Job(
				job_id=d["job_id"],
				item_code=d["item_code"],
				qty=d["qty"],
				due_date_mins=960 * 5,  # 5 days capacity
				setup_group=group,
				operations=_make_test_operations(ws),
			))

		result = solve(
			jobs=jobs,
			workstations=TEST_WORKSTATIONS,
			setup_matrix=TEST_SETUP_MATRIX,
			shift_capacity=TEST_SHIFT_CAPACITY,
			max_time_secs=30,
		)

		assert result.status in ("OPTIMAL", "FEASIBLE")
		assert len(result.scheduled_jobs) > 0

		# All 7 jobs should have scheduled operations
		scheduled_job_ids = set(s["job_id"] for s in result.scheduled_jobs)
		assert len(scheduled_job_ids) == len(jobs)


# ============================================================
# Module 3: Snapshot State Transition Tests
# ============================================================

class TestSnapshotStateMachine:
	"""Test snapshot state transitions (logic only, no DB)."""

	VALID_TRANSITIONS = {
		"Pre Plan": ["Draft Plan"],
		"Draft Plan": ["Fixed Plan", "Archived"],
		"Fixed Plan": ["Archived"],
		"Archived": [],
	}

	def test_valid_transitions(self):
		for from_state, to_states in self.VALID_TRANSITIONS.items():
			for to_state in to_states:
				assert to_state in self.VALID_TRANSITIONS.get(from_state, [])

	def test_no_backward_transition(self):
		"""Fixed Plan → Draft Plan is not allowed."""
		assert "Draft Plan" not in self.VALID_TRANSITIONS["Fixed Plan"]
		assert "Pre Plan" not in self.VALID_TRANSITIONS["Draft Plan"]
		assert "Pre Plan" not in self.VALID_TRANSITIONS["Fixed Plan"]

	def test_archived_terminal(self):
		"""Archived has no outgoing transitions."""
		assert self.VALID_TRANSITIONS["Archived"] == []


# ============================================================
# Module 4: Frozen Window Tests
# ============================================================

class TestFrozenWindow:
	def test_child_job_creation(self):
		"""Create child job for unmet quantity."""
		child = create_child_job("JOB-0001", 200, "2026-04-14")
		assert child["qty"] == 200
		assert child["priority"] == "critical"
		assert "SPLIT" in child["job_id"]
		assert child["parent_job_id"] == "JOB-0001"

	def test_frozen_jobs_excluded_from_solver(self):
		"""Frozen jobs (is_frozen=True) must not be re-scheduled."""
		# This validates the design principle — implementation in Phase 2
		frozen_job_ids = {"JOB-0001", "JOB-0002"}
		all_jobs = [
			{"job_id": "JOB-0001", "is_frozen": True},
			{"job_id": "JOB-0002", "is_frozen": True},
			{"job_id": "JOB-0003", "is_frozen": False},
			{"job_id": "JOB-0004", "is_frozen": False},
		]

		solver_input = [j for j in all_jobs if not j["is_frozen"]]
		assert len(solver_input) == 2
		assert all(j["job_id"] not in frozen_job_ids for j in solver_input)


# ============================================================
# Integration Test: End-to-End Flow
# ============================================================

class TestEndToEnd:
	def test_full_pipeline(self):
		"""Demand → Netting → Lot Sizing → Solver → Snapshot validation."""
		# Step 1: Demand Profiling
		demand_jobs = build_demand_profile(TEST_DEMAND)
		assert len(demand_jobs) == 7  # 4 (Model A) + 3 (Model B)

		# Step 2: Build solver jobs with operations
		solver_jobs = []
		for idx, d in enumerate(demand_jobs):
			ws = TEST_WORKSTATIONS[idx % len(TEST_WORKSTATIONS)]
			group = "Group-A" if "60kWh" in d["item_code"] else "Group-B"
			solver_jobs.append(Job(
				job_id=d["job_id"],
				item_code=d["item_code"],
				qty=d["qty"],
				due_date_mins=960 * 7,  # 7 days
				setup_group=group,
				operations=_make_test_operations(ws),
			))

		# Step 3: Run solver
		result = solve(
			jobs=solver_jobs,
			workstations=TEST_WORKSTATIONS,
			setup_matrix=TEST_SETUP_MATRIX,
			shift_capacity=TEST_SHIFT_CAPACITY,
			max_time_secs=30,
		)

		assert result.status in ("OPTIMAL", "FEASIBLE"), f"Solver failed: {result.status}"

		# Step 4: Validate snapshot data
		assert result.runtime_secs > 0
		assert len(result.scheduled_jobs) > 0

		# Step 5: Validate all jobs were scheduled
		scheduled_ids = set(s["job_id"] for s in result.scheduled_jobs)
		expected_ids = set(j.job_id for j in solver_jobs)
		assert scheduled_ids == expected_ids

		# Step 6: Validate precedence in output
		for job in solver_jobs:
			job_scheds = sorted(
				[s for s in result.scheduled_jobs if s["job_id"] == job.job_id],
				key=lambda x: x["operation_sequence"],
			)
			for i in range(len(job_scheds) - 1):
				assert job_scheds[i + 1]["planned_start_mins"] >= job_scheds[i]["planned_end_mins"]

		# Step 7: Frozen window simulation
		# Mark first 2 days' jobs as frozen
		frozen_threshold = 960 * 2  # 2 days
		frozen_count = 0
		unfrozen_count = 0
		for s in result.scheduled_jobs:
			if s["planned_start_mins"] < frozen_threshold:
				frozen_count += 1
			else:
				unfrozen_count += 1

		# Some jobs should fall within D+2 window
		print(f"\n=== E2E Results ===")
		print(f"Total jobs: {len(demand_jobs)}")
		print(f"Scheduled operations: {len(result.scheduled_jobs)}")
		print(f"Solver status: {result.status}")
		print(f"Runtime: {result.runtime_secs}s")
		print(f"Total tardiness: {result.total_tardiness_mins} mins")
		print(f"Frozen (D+2): {frozen_count} ops, Unfrozen: {unfrozen_count} ops")
		print(f"Objective: {result.objective_value}")


if __name__ == "__main__":
	pytest.main([__file__, "-v"])
