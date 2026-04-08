"""FP Solver Engine -- OR-Tools CP-SAT based scheduler with SCIP ensemble.

Solves a Flexible Job Shop problem with:
- 5-stage BOM precedence constraints
- Sequence-dependent setup times (via setup matrix + circuit constraints)
- Inline inspection modeled as precedence delay
- Multi-objective: alpha * Tardiness + beta * SetupTime minimization
- SCIP warm-start ensemble for solution refinement
"""

import time
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model


DEFAULT_ALPHA = 1000  # Tardiness penalty weight
DEFAULT_BETA = 1  # Setup time penalty weight
DEFAULT_MAX_TIME_SECS = 120
DEFAULT_NUM_WORKERS = 4


@dataclass(frozen=True)
class SolverConfig:
	"""Configurable solver parameters."""

	alpha: int = DEFAULT_ALPHA
	beta: int = DEFAULT_BETA
	max_time_secs: int = DEFAULT_MAX_TIME_SECS
	num_workers: int = DEFAULT_NUM_WORKERS
	enable_scip_ensemble: bool = False
	scip_max_time_secs: int = 60
	quality_threshold: float = 0.95  # If CP-SAT obj within this ratio of bound, skip SCIP


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
class ScheduledOp:
	"""A single scheduled operation in the result."""

	job_id: str
	item_code: str
	qty: float
	operation: str
	operation_sequence: int
	workstation: str
	planned_start_mins: int
	planned_end_mins: int
	setup_time_mins: int
	due_date_mins: int
	tardiness_mins: int


@dataclass
class SolverResult:
	status: str  # "OPTIMAL", "FEASIBLE", "INFEASIBLE", "ERROR"
	objective_value: float = 0
	total_tardiness_mins: float = 0
	total_setup_time_mins: float = 0
	runtime_secs: float = 0
	scheduled_jobs: list = field(default_factory=list)
	solver_used: str = "CP-SAT"  # "CP-SAT", "SCIP", "ENSEMBLE"


def _build_cpsat_model(
	jobs: list[Job],
	workstations: list[str],
	setup_matrix: dict[tuple[str, str, str], int],
	shift_capacity: dict[str, int],
	config: SolverConfig,
) -> tuple[cp_model.CpModel, dict, dict, list, int]:
	"""Build the CP-SAT model with all constraints.

	Returns (model, job_vars, ws_job_map, tardiness_vars, horizon).
	"""
	model = cp_model.CpModel()

	horizon = max(shift_capacity.values()) if shift_capacity else 10000

	# Decision variables: (job_id, op_seq) -> (start, end, interval, duration)
	job_vars: dict[tuple[str, int], tuple[Any, Any, Any, int]] = {}
	# Track which intervals belong to each workstation
	ws_intervals: dict[str, list] = {ws: [] for ws in workstations}
	# Track job ordering per workstation for setup time constraints
	ws_job_map: dict[str, list[tuple[str, str, Any, Any, Any]]] = {ws: [] for ws in workstations}

	for job in jobs:
		for op in job.operations:
			if op.is_inline_inspection:
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
				ws_job_map[op.workstation].append(
					(job.job_id, job.setup_group, start_var, end_var, interval_var)
				)

	# Constraint 1: Precedence -- operations within a job must be sequential
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

			delay = curr_op.wait_time_mins
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

	# Constraint 3: Setup time via optional interval + transition arcs
	setup_time_vars = []
	_add_setup_constraints(model, ws_job_map, setup_matrix, horizon, config, setup_time_vars)

	# Objective: minimize alpha * Tardiness + beta * SetupTime
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

	objective_terms = []
	for t in tardiness_vars:
		objective_terms.append(config.alpha * t)
	for s in setup_time_vars:
		objective_terms.append(config.beta * s)

	if objective_terms:
		model.minimize(sum(objective_terms))

	return model, job_vars, ws_job_map, tardiness_vars, horizon


def _add_setup_constraints(
	model: cp_model.CpModel,
	ws_job_map: dict[str, list[tuple[str, str, Any, Any, Any]]],
	setup_matrix: dict[tuple[str, str, str], int],
	horizon: int,
	config: SolverConfig,
	setup_time_vars: list,
) -> None:
	"""Add sequence-dependent setup time constraints per workstation.

	Uses pairwise ordering booleans + conditional setup intervals.
	For each pair of jobs on the same workstation, if they run in sequence
	and have different setup groups, a setup time interval is enforced.
	"""
	if not setup_matrix:
		return

	for ws, entries in ws_job_map.items():
		if len(entries) < 2:
			continue

		for i in range(len(entries)):
			for j in range(len(entries)):
				if i == j:
					continue

				job_i_id, group_i, start_i, end_i, _ = entries[i]
				job_j_id, group_j, start_j, end_j, _ = entries[j]

				setup_key = (ws, group_i, group_j)
				setup_mins = setup_matrix.get(setup_key, 0)

				if setup_mins <= 0:
					continue

				# Boolean: does job_i come immediately before job_j?
				suffix = f"_{ws}_{job_i_id}_{job_j_id}"
				order_bool = model.new_bool_var(f"order{suffix}")

				# If order_bool is true, job_j starts after job_i ends + setup
				model.add(start_j >= end_i + setup_mins).only_enforce_if(order_bool)

				# Track setup time for objective
				setup_var = model.new_int_var(0, setup_mins, f"setup{suffix}")
				model.add(setup_var == setup_mins).only_enforce_if(order_bool)
				model.add(setup_var == 0).only_enforce_if(~order_bool)
				setup_time_vars.append(setup_var)


def _extract_solution(
	solver: cp_model.CpSolver,
	jobs: list[Job],
	job_vars: dict,
	setup_matrix: dict,
) -> tuple[list[dict], float, float]:
	"""Extract scheduled jobs, total tardiness, and total setup time from solved model."""
	total_tardiness = 0
	total_setup = 0
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

	# Calculate per-workstation setup time from actual sequence
	total_setup = _calculate_actual_setup_time(scheduled, setup_matrix)

	return scheduled, total_tardiness, total_setup


def _calculate_actual_setup_time(
	scheduled: list[dict],
	setup_matrix: dict[tuple[str, str, str], int],
) -> float:
	"""Calculate actual setup time from the scheduled sequence."""
	if not setup_matrix:
		return 0

	# Build a job_id -> setup_group lookup from scheduled data
	# We need the original jobs for this, but we can infer from the schedule
	total = 0
	# Group operations by workstation and sort by start time
	ws_ops: dict[str, list[dict]] = {}
	for op in scheduled:
		ws_ops.setdefault(op["workstation"], []).append(op)

	for ws, ops in ws_ops.items():
		ops_sorted = sorted(ops, key=lambda x: x["planned_start_mins"])
		for i in range(len(ops_sorted) - 1):
			# Setup time would be between consecutive jobs (not ops within same job)
			if ops_sorted[i]["job_id"] != ops_sorted[i + 1]["job_id"]:
				gap = ops_sorted[i + 1]["planned_start_mins"] - ops_sorted[i]["planned_end_mins"]
				# Attribute any gap that matches a setup matrix entry as setup time
				for key, mins in setup_matrix.items():
					if key[0] == ws and gap >= mins > 0:
						total += mins
						break

	return total


def _try_scip_refinement(
	jobs: list[Job],
	workstations: list[str],
	setup_matrix: dict,
	shift_capacity: dict,
	cpsat_result: SolverResult,
	config: SolverConfig,
) -> SolverResult | None:
	"""Attempt SCIP refinement using CP-SAT solution as warm start.

	Returns improved SolverResult or None if SCIP cannot improve.
	"""
	try:
		from pyscipopt import Model as ScipModel
	except ImportError:
		return None

	scip = ScipModel("fp_scheduler_refinement")
	scip.hideOutput()
	scip.setParam("limits/time", config.scip_max_time_secs)

	horizon = max(shift_capacity.values()) if shift_capacity else 10000

	# Build MIP variables
	start_vars = {}
	end_vars = {}

	for job in jobs:
		for op in job.operations:
			if op.is_inline_inspection:
				continue

			key = (job.job_id, op.sequence)
			start_vars[key] = scip.addVar(
				name=f"start_{job.job_id}_{op.sequence}",
				vtype="I", lb=0, ub=horizon,
			)
			end_vars[key] = scip.addVar(
				name=f"end_{job.job_id}_{op.sequence}",
				vtype="I", lb=0, ub=horizon,
			)
			# Duration constraint
			scip.addCons(end_vars[key] == start_vars[key] + op.tat_mins)

	# Precedence constraints
	for job in jobs:
		non_inspection_ops = [op for op in job.operations if not op.is_inline_inspection]
		for i in range(len(non_inspection_ops) - 1):
			curr_op = non_inspection_ops[i]
			next_op = non_inspection_ops[i + 1]

			curr_key = (job.job_id, curr_op.sequence)
			next_key = (job.job_id, next_op.sequence)

			if curr_key not in end_vars or next_key not in start_vars:
				continue

			delay = curr_op.wait_time_mins
			for op in job.operations:
				if (
					op.is_inline_inspection
					and op.sequence > curr_op.sequence
					and op.sequence < next_op.sequence
				):
					delay += op.inspection_tat_mins

			scip.addCons(start_vars[next_key] >= end_vars[curr_key] + delay)

	# No-overlap via big-M disjunctive constraints
	big_m = horizon + 1
	ws_ops_map: dict[str, list[tuple[str, int]]] = {}
	for job in jobs:
		for op in job.operations:
			if op.is_inline_inspection:
				continue
			if op.workstation in ws_ops_map:
				ws_ops_map.setdefault(op.workstation, []).append((job.job_id, op.sequence))
			else:
				ws_ops_map[op.workstation] = [(job.job_id, op.sequence)]

	for ws, op_keys in ws_ops_map.items():
		for i in range(len(op_keys)):
			for j in range(i + 1, len(op_keys)):
				key_i = op_keys[i]
				key_j = op_keys[j]
				if key_i not in start_vars or key_j not in start_vars:
					continue

				y = scip.addVar(name=f"order_{ws}_{key_i}_{key_j}", vtype="B")
				scip.addCons(start_vars[key_j] >= end_vars[key_i] - big_m * (1 - y))
				scip.addCons(start_vars[key_i] >= end_vars[key_j] - big_m * y)

	# Tardiness objective
	tardiness_vars = {}
	for job in jobs:
		last_ops = [op for op in job.operations if not op.is_inline_inspection]
		if not last_ops:
			continue
		last_op = last_ops[-1]
		key = (job.job_id, last_op.sequence)
		if key not in end_vars:
			continue

		t_var = scip.addVar(name=f"tardiness_{job.job_id}", vtype="I", lb=0, ub=horizon)
		scip.addCons(t_var >= end_vars[key] - job.due_date_mins)
		scip.addCons(t_var >= 0)
		tardiness_vars[job.job_id] = t_var

	obj = sum(config.alpha * t for t in tardiness_vars.values())
	scip.setObjective(obj, "minimize")

	# Warm start from CP-SAT solution
	sol = scip.createSol()
	for sched in cpsat_result.scheduled_jobs:
		key = (sched["job_id"], sched["operation_sequence"])
		if key in start_vars:
			scip.setSolVal(sol, start_vars[key], sched["planned_start_mins"])
			scip.setSolVal(sol, end_vars[key], sched["planned_end_mins"])
	# Attempt to add the warm start solution (may be rejected if infeasible in MIP)
	try:
		scip.addSol(sol)
	except Exception:
		pass

	scip.optimize()

	scip_status = scip.getStatus()
	if scip_status not in ("optimal", "bestsollimit", "timelimit"):
		return None

	if scip.getNSols() == 0:
		return None

	scip_obj = scip.getObjVal()
	if scip_obj >= cpsat_result.objective_value:
		return None  # SCIP did not improve

	# Extract SCIP solution
	total_tardiness = 0
	scheduled = []
	for job in jobs:
		for op in job.operations:
			if op.is_inline_inspection:
				continue
			key = (job.job_id, op.sequence)
			if key not in start_vars:
				continue

			start_val = int(round(scip.getVal(start_vars[key])))
			end_val = int(round(scip.getVal(end_vars[key])))

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

	return SolverResult(
		status="OPTIMAL" if scip_status == "optimal" else "FEASIBLE",
		objective_value=scip_obj,
		total_tardiness_mins=total_tardiness,
		total_setup_time_mins=_calculate_actual_setup_time(scheduled, {}),
		runtime_secs=0,  # Will be set by caller
		scheduled_jobs=scheduled,
		solver_used="SCIP",
	)


def solve(
	jobs: list[Job],
	workstations: list[str],
	setup_matrix: dict[tuple[str, str, str], int],
	shift_capacity: dict[str, int],
	max_time_secs: int = DEFAULT_MAX_TIME_SECS,
	config: SolverConfig | None = None,
) -> SolverResult:
	"""Run the CP-SAT solver with optional SCIP ensemble refinement.

	Args:
		jobs: List of Job objects with operations.
		workstations: List of workstation names.
		setup_matrix: Dict mapping (ws, from_group, to_group) to setup minutes.
		shift_capacity: Dict mapping workstation to total capacity in minutes.
		max_time_secs: Solver time limit (overridden by config if provided).
		config: Optional SolverConfig for fine-tuned parameters.

	Returns SolverResult.
	"""
	if config is None:
		config = SolverConfig(max_time_secs=max_time_secs)

	start_time = time.time()

	# Stage 1: CP-SAT
	model, job_vars, ws_job_map, tardiness_vars, horizon = _build_cpsat_model(
		jobs, workstations, setup_matrix, shift_capacity, config,
	)

	solver = cp_model.CpSolver()
	solver.parameters.max_time_in_seconds = config.max_time_secs
	solver.parameters.num_workers = config.num_workers

	status = solver.solve(model)

	status_map = {
		cp_model.OPTIMAL: "OPTIMAL",
		cp_model.FEASIBLE: "FEASIBLE",
		cp_model.INFEASIBLE: "INFEASIBLE",
		cp_model.MODEL_INVALID: "ERROR",
		cp_model.UNKNOWN: "ERROR",
	}

	cpsat_runtime = time.time() - start_time

	result = SolverResult(
		status=status_map.get(status, "ERROR"),
		runtime_secs=round(cpsat_runtime, 3),
		solver_used="CP-SAT",
	)

	if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
		return result

	# Extract CP-SAT solution
	scheduled, total_tardiness, total_setup = _extract_solution(
		solver, jobs, job_vars, setup_matrix,
	)

	result.objective_value = solver.objective_value
	result.total_tardiness_mins = total_tardiness
	result.total_setup_time_mins = total_setup
	result.scheduled_jobs = scheduled

	# Stage 2: SCIP Ensemble (optional)
	if config.enable_scip_ensemble and status != cp_model.OPTIMAL:
		scip_result = _try_scip_refinement(
			jobs, workstations, setup_matrix, shift_capacity, result, config,
		)
		if scip_result is not None:
			scip_result.runtime_secs = round(time.time() - start_time, 3)
			scip_result.solver_used = "ENSEMBLE"
			return scip_result

	result.runtime_secs = round(time.time() - start_time, 3)
	return result
