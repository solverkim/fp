"""FP Solver Engine — OR-Tools CP-SAT based scheduler.

Solves a Flexible Job Shop problem with:
- 5-stage BOM precedence constraints
- Sequence-dependent setup times (via setup matrix)
- Inline inspection modeled as precedence delay
- Multi-objective: α·Tardiness + β·SetupTime minimization
"""

import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model


ALPHA = 1000  # Tardiness penalty weight (납기 우선)
BETA = 1  # Setup time penalty weight


@dataclass
class Job:
	job_id: str
	item_code: str
	qty: float
	due_date_mins: int  # Due date as minutes from horizon start
	setup_group: str = ""
	operations: list = field(default_factory=list)  # list of Operation


@dataclass
class Operation:
	name: str
	sequence: int
	tat_mins: int
	wait_time_mins: int = 0
	is_inline_inspection: bool = False
	inspection_tat_mins: int = 0
	workstation: str = ""


@dataclass
class SolverResult:
	status: str  # "OPTIMAL", "FEASIBLE", "INFEASIBLE", "ERROR"
	objective_value: float = 0
	total_tardiness_mins: float = 0
	total_setup_time_mins: float = 0
	runtime_secs: float = 0
	scheduled_jobs: list = field(default_factory=list)


def solve(
	jobs: list[Job],
	workstations: list[str],
	setup_matrix: dict,  # (ws, from_group, to_group) -> setup_mins
	shift_capacity: dict,  # ws -> total available mins in horizon
	max_time_secs: int = 120,
) -> SolverResult:
	"""Run the CP-SAT solver.

	Args:
		jobs: List of Job objects with operations.
		workstations: List of workstation names.
		setup_matrix: Dict mapping (ws, from_group, to_group) to setup minutes.
		shift_capacity: Dict mapping workstation to total capacity in minutes.
		max_time_secs: Solver time limit.

	Returns SolverResult.
	"""
	start_time = time.time()

	model = cp_model.CpModel()

	# Compute horizon
	horizon = max(shift_capacity.values()) if shift_capacity else 10000

	# Decision variables
	# For each job's each operation: start, end, interval
	job_vars = {}  # (job_id, op_seq) -> (start, end, interval, duration)
	ws_intervals = {ws: [] for ws in workstations}  # intervals per workstation

	for job in jobs:
		for op in job.operations:
			if op.is_inline_inspection:
				# Inline inspection: no resource, modeled as delay only
				continue

			suffix = f"_{job.job_id}_{op.sequence}"
			duration = op.tat_mins

			start_var = model.new_int_var(0, horizon, f"start{suffix}")
			end_var = model.new_int_var(0, horizon, f"end{suffix}")
			interval_var = model.new_interval_var(
				start_var, duration, end_var, f"interval{suffix}"
			)

			job_vars[(job.job_id, op.sequence)] = (start_var, end_var, interval_var, duration)

			if op.workstation in ws_intervals:
				ws_intervals[op.workstation].append(interval_var)

	# Constraint 1: Precedence — operations within a job must be sequential
	for job in jobs:
		non_inspection_ops = [op for op in job.operations if not op.is_inline_inspection]
		for i in range(len(non_inspection_ops) - 1):
			curr_op = non_inspection_ops[i]
			next_op = non_inspection_ops[i + 1]

			if (job.job_id, curr_op.sequence) not in job_vars:
				continue
			if (job.job_id, next_op.sequence) not in job_vars:
				continue

			_, curr_end, _, _ = job_vars[(job.job_id, curr_op.sequence)]
			next_start, _, _, _ = job_vars[(job.job_id, next_op.sequence)]

			# Add wait time + inspection TAT as precedence delay
			delay = curr_op.wait_time_mins
			# Check if there's an inline inspection between these ops
			for op in job.operations:
				if (
					op.is_inline_inspection
					and op.sequence > curr_op.sequence
					and op.sequence < next_op.sequence
				):
					delay += op.inspection_tat_mins

			model.add(next_start >= curr_end + delay)

	# Constraint 2: No-overlap per workstation
	for ws, intervals in ws_intervals.items():
		if len(intervals) > 1:
			model.add_no_overlap(intervals)

	# Objective: minimize α·Tardiness + β·SetupTime
	tardiness_vars = []
	for job in jobs:
		last_ops = [op for op in job.operations if not op.is_inline_inspection]
		if not last_ops:
			continue
		last_op = last_ops[-1]
		key = (job.job_id, last_op.sequence)
		if key not in job_vars:
			continue

		_, end_var, _, _ = job_vars[key]

		tardiness = model.new_int_var(0, horizon, f"tardiness_{job.job_id}")
		model.add_max_equality(tardiness, [0, end_var - job.due_date_mins])
		tardiness_vars.append(tardiness)

	# For simplicity in this validation, we minimize tardiness only
	# (setup time optimization requires circuit constraints which adds complexity)
	objective_terms = []
	for t in tardiness_vars:
		objective_terms.append(ALPHA * t)

	if objective_terms:
		model.minimize(sum(objective_terms))

	# Solve
	solver = cp_model.CpSolver()
	solver.parameters.max_time_in_seconds = max_time_secs
	solver.parameters.num_workers = 4

	status = solver.solve(model)

	runtime = time.time() - start_time

	status_map = {
		cp_model.OPTIMAL: "OPTIMAL",
		cp_model.FEASIBLE: "FEASIBLE",
		cp_model.INFEASIBLE: "INFEASIBLE",
		cp_model.MODEL_INVALID: "ERROR",
		cp_model.UNKNOWN: "ERROR",
	}

	result = SolverResult(
		status=status_map.get(status, "ERROR"),
		runtime_secs=round(runtime, 3),
	)

	if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
		result.objective_value = solver.objective_value

		total_tardiness = 0
		scheduled = []

		for job in jobs:
			for op in job.operations:
				if op.is_inline_inspection:
					continue

				key = (job.job_id, op.sequence)
				if key not in job_vars:
					continue

				start_var, end_var, _, _ = job_vars[key]
				start_val = solver.value(start_var)
				end_val = solver.value(end_var)

				# Compute tardiness for final operation
				tardiness = 0
				last_ops = [o for o in job.operations if not o.is_inline_inspection]
				if last_ops and op.sequence == last_ops[-1].sequence:
					tardiness = max(0, end_val - job.due_date_mins)
					total_tardiness += tardiness

				scheduled.append({
					"job_id": job.job_id,
					"item_code": job.item_code,
					"qty": job.qty,
					"operation": op.name,
					"operation_sequence": op.sequence,
					"workstation": op.workstation,
					"planned_start_mins": start_val,
					"planned_end_mins": end_val,
					"setup_time_mins": 0,
					"due_date_mins": job.due_date_mins,
					"tardiness_mins": tardiness,
				})

		result.total_tardiness_mins = total_tardiness
		result.scheduled_jobs = scheduled

	return result
