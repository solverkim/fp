"""Tests for solver modules: data_loader helpers, result_writer helpers,
runner pipeline, and 500+ job benchmark.

These tests exercise pure-Python logic without Frappe DB.  Frappe-dependent
functions are tested through lightweight mocks.
"""

import json
import time
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from fp.demand.netting import build_demand_profile
from fp.solver.engine import Job, Operation, SolverConfig, SolverResult, solve


# ============================================================
# Shared Fixtures
# ============================================================

TEST_OPERATIONS_DATA = [
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

TEST_SHIFT_CAPACITY = {"Line-1": 960, "Line-2": 960}


def _make_operations(workstation: str) -> list[Operation]:
    ops = []
    for d in TEST_OPERATIONS_DATA:
        ops.append(Operation(
            name=d["name"],
            sequence=d["sequence"],
            tat_mins=d["tat_mins"],
            wait_time_mins=d.get("wait_time_mins", 0),
            is_inline_inspection=d.get("is_inline_inspection", False),
            inspection_tat_mins=d.get("inspection_tat_mins", 0),
            workstation=workstation,
        ))
    return ops


def _make_job(job_id: str, item_code: str, setup_group: str, ws: str, due: int = 2000, qty: int = 200) -> Job:
    return Job(
        job_id=job_id,
        item_code=item_code,
        qty=qty,
        due_date_mins=due,
        setup_group=setup_group,
        operations=_make_operations(ws),
    )


# ============================================================
# 1. Data Loader — Pure Helper Tests
# ============================================================


class TestDataLoaderHelpers:
    """Test date conversion and operation building helpers."""

    def test_parse_date_string(self):
        from fp.solver.data_loader import _parse_date
        result = _parse_date("2026-04-14")
        assert result == datetime(2026, 4, 14)

    def test_parse_date_object(self):
        from fp.solver.data_loader import _parse_date
        result = _parse_date(date(2026, 4, 14))
        assert result == datetime(2026, 4, 14)

    def test_parse_datetime_object(self):
        from fp.solver.data_loader import _parse_date
        dt = datetime(2026, 4, 14, 8, 30)
        result = _parse_date(dt)
        assert result == dt

    def test_date_to_minutes(self):
        from fp.solver.data_loader import _date_to_minutes
        start = datetime(2026, 4, 7, 0, 0)
        target = datetime(2026, 4, 8, 0, 0)  # +1 day
        assert _date_to_minutes(target, start) == 1440

    def test_date_to_minutes_same_day(self):
        from fp.solver.data_loader import _date_to_minutes
        start = datetime(2026, 4, 7, 0, 0)
        assert _date_to_minutes(start, start) == 0

    def test_build_operations_from_tat_only(self):
        """When no routing exists, fall back to TAT Master entries."""
        from fp.solver.data_loader import _build_operations

        tat_map = {
            ("BM-60kWh-A", "Assembly"): {
                "workstation": "Line-1",
                "base_tat_mins": 30,
                "wait_time_mins": 5,
                "is_inline_inspection": False,
                "inspection_tat_mins": 0,
            },
            ("BM-60kWh-A", "Welding"): {
                "workstation": "Line-1",
                "base_tat_mins": 25,
                "wait_time_mins": 0,
                "is_inline_inspection": False,
                "inspection_tat_mins": 0,
            },
        }
        routing_map = {}  # No routing
        workstations = ["Line-1"]

        ops = _build_operations("BM-60kWh-A", tat_map, routing_map, workstations)
        assert len(ops) == 2
        assert ops[0].name == "Assembly"
        assert ops[0].tat_mins == 30
        assert ops[1].name == "Welding"

    def test_build_operations_with_routing(self):
        """When BOM routing exists, use routing sequence with TAT enrichment."""
        from fp.solver.data_loader import _build_operations

        tat_map = {
            ("BM-60kWh-A", "Welding"): {
                "workstation": "Line-1",
                "base_tat_mins": 25,
                "wait_time_mins": 0,
                "is_inline_inspection": False,
                "inspection_tat_mins": 0,
            },
            ("BM-60kWh-A", "Assembly"): {
                "workstation": "Line-1",
                "base_tat_mins": 30,
                "wait_time_mins": 5,
                "is_inline_inspection": False,
                "inspection_tat_mins": 0,
            },
        }
        routing_map = {
            "BM-60kWh-A": [
                {"operation": "Assembly", "sequence": 10},
                {"operation": "Welding", "sequence": 20},
            ],
        }
        workstations = ["Line-1"]

        ops = _build_operations("BM-60kWh-A", tat_map, routing_map, workstations)
        assert len(ops) == 2
        assert ops[0].sequence == 10
        assert ops[0].name == "Assembly"
        assert ops[1].sequence == 20

    def test_build_operations_missing_tat_defaults(self):
        """Operations with missing TAT entries get zero-valued defaults."""
        from fp.solver.data_loader import _build_operations

        tat_map = {}  # No TAT data
        routing_map = {
            "BM-X": [{"operation": "OpA", "sequence": 10}],
        }

        ops = _build_operations("BM-X", tat_map, routing_map, ["Line-1"])
        assert len(ops) == 1
        assert ops[0].tat_mins == 0
        assert ops[0].workstation == "Line-1"  # Falls back to first workstation


# ============================================================
# 2. Result Writer — Pure Helper Tests
# ============================================================


class TestResultWriterHelpers:
    def test_mins_to_datetime(self):
        from fp.solver.result_writer import _mins_to_datetime
        start = datetime(2026, 4, 7, 0, 0)
        result = _mins_to_datetime(90, start)
        assert result == datetime(2026, 4, 7, 1, 30)

    def test_mins_to_date(self):
        from fp.solver.result_writer import _mins_to_date
        start = datetime(2026, 4, 7, 0, 0)
        result = _mins_to_date(1440, start)
        assert result == date(2026, 4, 8)

    def test_calculate_utilization_empty(self):
        from fp.solver.result_writer import _calculate_utilization
        result = SolverResult(status="OPTIMAL", scheduled_jobs=[])
        util = _calculate_utilization(result, date(2026, 4, 7), date(2026, 4, 14))
        assert util == 0.0

    def test_calculate_utilization_with_jobs(self):
        from fp.solver.result_writer import _calculate_utilization
        result = SolverResult(
            status="OPTIMAL",
            scheduled_jobs=[
                {"workstation": "Line-1", "planned_start_mins": 0, "planned_end_mins": 480},
                {"workstation": "Line-1", "planned_start_mins": 480, "planned_end_mins": 960},
            ],
        )
        # No Frappe DB — fallback to max_end * num_ws = 960 * 1 = 960
        with patch("fp.solver.result_writer._get_total_capacity", side_effect=Exception):
            util = _calculate_utilization(result, date(2026, 4, 7), date(2026, 4, 14))
        assert util == 100.0  # 960/960 = 100%


# ============================================================
# 3. SolverConfig Tests
# ============================================================


class TestSolverConfig:
    def test_default_config(self):
        cfg = SolverConfig()
        assert cfg.alpha == 1000
        assert cfg.beta == 1
        assert cfg.max_time_secs == 120
        assert cfg.enable_scip_ensemble is False

    def test_custom_config(self):
        cfg = SolverConfig(alpha=500, beta=10, max_time_secs=60)
        assert cfg.alpha == 500
        assert cfg.beta == 10
        assert cfg.max_time_secs == 60

    def test_config_immutable(self):
        cfg = SolverConfig()
        with pytest.raises(AttributeError):
            cfg.alpha = 999


# ============================================================
# 4. Runner — Mock-based Integration Tests
# ============================================================


class TestRunner:
    """Test the runner pipeline with mocked Frappe calls."""

    def _mock_frappe_module(self):
        """Set up frappe mock for runner tests."""
        mock_frappe = MagicMock()
        mock_frappe.session.user = "Administrator"
        mock_frappe.utils.now.return_value = "2026-04-08 12:00:00"
        return mock_frappe

    @patch("fp.solver.runner.frappe")
    @patch("fp.solver.runner.load_solver_inputs")
    @patch("fp.solver.runner.load_solver_config_from_doctype")
    @patch("fp.solver.runner.solve")
    @patch("fp.solver.runner.build_master_snapshot")
    @patch("fp.solver.runner.write_snapshot")
    def test_run_solver_success(
        self, mock_write, mock_master, mock_solve, mock_config, mock_inputs, mock_frappe,
    ):
        jobs = [_make_job("JOB-0001", "BM-60kWh-A", "Group-A", "Line-1")]
        mock_inputs.return_value = (jobs, ["Line-1"], TEST_SETUP_MATRIX, TEST_SHIFT_CAPACITY)
        mock_config.return_value = SolverConfig(max_time_secs=10)
        mock_solve.return_value = SolverResult(
            status="OPTIMAL",
            objective_value=0,
            total_tardiness_mins=0,
            total_setup_time_mins=0,
            runtime_secs=1.5,
            scheduled_jobs=[{"job_id": "JOB-0001", "planned_start_mins": 0, "planned_end_mins": 110}],
            solver_used="CP-SAT",
        )
        mock_master.return_value = {"generated_at": "now"}
        mock_write.return_value = "PP-0001"

        from fp.solver.runner import run_solver

        result = run_solver(
            demand_jobs=[{"job_id": "JOB-0001", "item_code": "BM-60kWh-A", "qty": 200, "due_date": "2026-04-14"}],
            horizon_start=date(2026, 4, 7),
            horizon_end=date(2026, 4, 14),
            snapshot_name="Test Run",
        )

        assert result["snapshot_id"] == "PP-0001"
        assert result["status"] == "OPTIMAL"
        assert result["total_jobs"] == 1
        mock_write.assert_called_once()

    @patch("fp.solver.runner.frappe")
    @patch("fp.solver.runner.load_solver_inputs")
    def test_run_solver_no_jobs(self, mock_inputs, mock_frappe):
        mock_inputs.return_value = ([], ["Line-1"], {}, TEST_SHIFT_CAPACITY)

        from fp.solver.runner import run_solver

        result = run_solver(
            demand_jobs=[],
            horizon_start=date(2026, 4, 7),
            horizon_end=date(2026, 4, 14),
            snapshot_name="Empty Run",
        )

        assert result["status"] == "ERROR"
        assert result["snapshot_id"] is None

    @patch("fp.solver.runner.frappe")
    @patch("fp.solver.runner.load_solver_inputs")
    @patch("fp.solver.runner.load_solver_config_from_doctype")
    @patch("fp.solver.runner.solve")
    def test_run_solver_infeasible(self, mock_solve, mock_config, mock_inputs, mock_frappe):
        jobs = [_make_job("JOB-0001", "BM-60kWh-A", "Group-A", "Line-1")]
        mock_inputs.return_value = (jobs, ["Line-1"], {}, TEST_SHIFT_CAPACITY)
        mock_config.return_value = SolverConfig(max_time_secs=5)
        mock_solve.return_value = SolverResult(status="INFEASIBLE", runtime_secs=0.5)

        from fp.solver.runner import run_solver

        result = run_solver(
            demand_jobs=[{"job_id": "JOB-0001"}],
            horizon_start=date(2026, 4, 7),
            horizon_end=date(2026, 4, 14),
            snapshot_name="Infeasible Run",
        )

        assert result["status"] == "INFEASIBLE"
        assert result["snapshot_id"] is None


# ============================================================
# 5. Engine — 500+ Job Benchmark
# ============================================================


class TestBenchmark:
    """Benchmark the solver with 500+ jobs to verify performance."""

    @pytest.mark.parametrize("num_jobs", [100, 250, 500])
    def test_solver_scales(self, num_jobs):
        """Solver should find a feasible solution for N jobs within 60s."""
        workstations = [f"Line-{i}" for i in range(1, 5)]  # 4 lines
        shift_capacity = {ws: 960 * 7 for ws in workstations}  # 7 days

        jobs = []
        for i in range(num_jobs):
            ws = workstations[i % len(workstations)]
            group = "Group-A" if i % 2 == 0 else "Group-B"
            jobs.append(Job(
                job_id=f"JOB-{i:04d}",
                item_code=f"BM-{group}",
                qty=200,
                due_date_mins=960 * 7,
                setup_group=group,
                operations=[
                    Operation(name="Op1", sequence=10, tat_mins=15, workstation=ws),
                    Operation(name="Op2", sequence=20, tat_mins=10, workstation=ws),
                    Operation(name="Op3", sequence=30, tat_mins=12, workstation=ws),
                ],
            ))

        setup_matrix = {}
        for ws in workstations:
            setup_matrix[(ws, "Group-A", "Group-B")] = 20
            setup_matrix[(ws, "Group-B", "Group-A")] = 25

        t0 = time.time()
        result = solve(
            jobs=jobs,
            workstations=workstations,
            setup_matrix=setup_matrix,
            shift_capacity=shift_capacity,
            max_time_secs=60,
        )
        elapsed = time.time() - t0

        assert result.status in ("OPTIMAL", "FEASIBLE"), (
            f"Solver failed with {num_jobs} jobs: {result.status}"
        )
        assert len(result.scheduled_jobs) > 0

        scheduled_ids = set(s["job_id"] for s in result.scheduled_jobs)
        assert len(scheduled_ids) == num_jobs, (
            f"Expected {num_jobs} jobs scheduled, got {len(scheduled_ids)}"
        )

        print(f"\n  [{num_jobs} jobs] status={result.status}, "
              f"time={elapsed:.1f}s, tardiness={result.total_tardiness_mins}")

    def test_500_jobs_full_bom(self):
        """500 jobs with 5-stage BOM — the core benchmark from MESA-48."""
        workstations = ["Line-1", "Line-2", "Line-3", "Line-4"]
        # 21 days capacity provides enough headroom for 500 jobs with
        # 4 non-inspection ops each (~55K mins) + wait/inspection delays
        shift_capacity = {ws: 960 * 21 for ws in workstations}

        # Setup matrix for all 4 lines
        setup_matrix_4ws = {}
        for ws in workstations:
            setup_matrix_4ws[(ws, "Group-A", "Group-B")] = 45
            setup_matrix_4ws[(ws, "Group-B", "Group-A")] = 60

        jobs = []
        for i in range(500):
            ws = workstations[i % len(workstations)]
            group = "Group-A" if i % 3 != 0 else "Group-B"
            jobs.append(Job(
                job_id=f"JOB-{i:04d}",
                item_code=f"BM-{group}",
                qty=200,
                due_date_mins=960 * 21,
                setup_group=group,
                operations=_make_operations(ws),
            ))

        t0 = time.time()
        result = solve(
            jobs=jobs,
            workstations=workstations,
            setup_matrix=setup_matrix_4ws,
            shift_capacity=shift_capacity,
            max_time_secs=60,
        )
        elapsed = time.time() - t0

        assert result.status in ("OPTIMAL", "FEASIBLE"), (
            f"500-job benchmark failed: {result.status}"
        )

        scheduled_ids = set(s["job_id"] for s in result.scheduled_jobs)
        assert len(scheduled_ids) == 500

        # Verify precedence
        for i in range(500):
            job_id = f"JOB-{i:04d}"
            job_ops = sorted(
                [s for s in result.scheduled_jobs if s["job_id"] == job_id],
                key=lambda x: x["operation_sequence"],
            )
            for j in range(len(job_ops) - 1):
                assert job_ops[j + 1]["planned_start_mins"] >= job_ops[j]["planned_end_mins"], (
                    f"Precedence violation in {job_id}"
                )

        print(f"\n  [500 jobs, 5-stage BOM] status={result.status}, "
              f"time={elapsed:.1f}s, obj={result.objective_value}, "
              f"tardiness={result.total_tardiness_mins}")


# ============================================================
# 6. Solver Config Doctype — Mock Test
# ============================================================


class TestSolverConfigDoctype:
    @patch("fp.factory_planner.doctype.fp_solver_config.fp_solver_config.frappe")
    def test_get_solver_config_defaults(self, mock_frappe):
        mock_frappe.get_single.side_effect = Exception("Not found")

        from fp.factory_planner.doctype.fp_solver_config.fp_solver_config import get_solver_config
        cfg = get_solver_config()
        assert cfg["alpha"] == 1000
        assert cfg["max_time_secs"] == 120

    @patch("fp.factory_planner.doctype.fp_solver_config.fp_solver_config.frappe")
    def test_get_solver_config_from_doc(self, mock_frappe):
        mock_doc = MagicMock()
        mock_doc.alpha = 500
        mock_doc.beta = 5
        mock_doc.max_time_secs = 90
        mock_doc.num_workers = 8
        mock_doc.enable_scip_ensemble = 1
        mock_doc.scip_max_time_secs = 30
        mock_doc.quality_threshold = 0.9
        mock_frappe.get_single.return_value = mock_doc

        from fp.factory_planner.doctype.fp_solver_config.fp_solver_config import get_solver_config
        cfg = get_solver_config()
        assert cfg["alpha"] == 500
        assert cfg["beta"] == 5
        assert cfg["num_workers"] == 8
        assert cfg["enable_scip_ensemble"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
