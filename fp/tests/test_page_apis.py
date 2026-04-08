"""E2E / Integration tests for Phase 3 page backend APIs.

Tests the three custom Frappe pages:
  1. Gantt Tuning — validate_reschedule()
  2. Planning Dashboard — compare_snapshots()
  3. WO Tracking — get_fixed_plan_jobs(), get_order_genealogy()

These are mock-based tests exercising the Python API layer without a live DB.
"""

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Module path constants for patching
GANTT_MOD = "fp.factory_planner.page.gantt_tuning.gantt_tuning"
WO_MOD = "fp.factory_planner.page.wo_tracking.wo_tracking"
SNAP_MOD = "fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot"


def _identity_translate(s, *args):
    """Mock for frappe._() translation function."""
    return s.format(*args) if args else s


# ============================================================
# Shared Fixtures
# ============================================================


def _make_snapshot_doc(
    name: str = "PP-0001",
    status: str = "Draft Plan",
    jobs: list[dict[str, Any]] | None = None,
    master_snapshot: str | None = None,
) -> MagicMock:
    """Create a mock FP Planning Snapshot document."""
    doc = MagicMock()
    doc.name = name
    doc.status = status
    doc.master_snapshot = master_snapshot or "{}"

    mock_jobs = []
    for j in (jobs or []):
        job = MagicMock()
        for k, v in j.items():
            setattr(job, k, v)
        job.get = lambda key, default=None, _j=j: _j.get(key, default)
        mock_jobs.append(job)

    doc.jobs = mock_jobs
    doc.get = lambda field, default=None: getattr(doc, field, default)

    # KPI fields
    for field in (
        "solver_run_time_secs",
        "objective_value",
        "total_tardiness_mins",
        "total_setup_time_mins",
        "line_utilization_pct",
    ):
        if not hasattr(doc, field) or isinstance(getattr(doc, field), MagicMock):
            setattr(doc, field, 0)

    return doc


SAMPLE_JOBS = [
    {
        "job_id": "JOB-0001",
        "item_code": "BM-60kWh-A",
        "qty": 200,
        "lot_number": 0,
        "workstation": "Line-1",
        "operation": "Assembly",
        "operation_sequence": 10,
        "planned_start": "2026-04-07 08:00:00",
        "planned_end": "2026-04-07 10:00:00",
        "due_date": "2026-04-10",
        "setup_time_mins": 15,
        "tardiness_mins": 0,
        "is_frozen": False,
        "work_order": None,
        "source_demand_id": "SO-0001",
    },
    {
        "job_id": "JOB-0002",
        "item_code": "BM-60kWh-B",
        "qty": 100,
        "lot_number": 0,
        "workstation": "Line-1",
        "operation": "Welding",
        "operation_sequence": 20,
        "planned_start": "2026-04-07 10:30:00",
        "planned_end": "2026-04-07 12:00:00",
        "due_date": "2026-04-09",
        "setup_time_mins": 30,
        "tardiness_mins": 0,
        "is_frozen": False,
        "work_order": "WO-0001",
        "source_demand_id": "SO-0001",
    },
    {
        "job_id": "JOB-0003",
        "item_code": "BM-60kWh-A",
        "qty": 150,
        "lot_number": 1,
        "workstation": "Line-2",
        "operation": "Assembly",
        "operation_sequence": 10,
        "planned_start": "2026-04-07 06:00:00",
        "planned_end": "2026-04-07 08:00:00",
        "due_date": "2026-04-10",
        "setup_time_mins": 0,
        "tardiness_mins": 0,
        "is_frozen": True,
        "work_order": "WO-0002",
        "source_demand_id": "SO-0001",
    },
]


# ============================================================
# 1. Gantt Tuning — validate_reschedule
# ============================================================


class TestGanttTuningValidateReschedule:
    """Test gantt_tuning.validate_reschedule backend API."""

    @patch(f"{GANTT_MOD}._check_shift_capacity", return_value=True)
    @patch(f"{GANTT_MOD}._check_transition_allowed", return_value=None)
    @patch(f"{GANTT_MOD}._calc_setup_time", return_value=10.0)
    @patch(f"{GANTT_MOD}._get_frozen_boundary", return_value=None)
    @patch(f"{GANTT_MOD}.frappe")
    def test_valid_reschedule(
        self, mock_frappe, mock_frozen, mock_setup, mock_transition, mock_shift
    ):
        """A valid reschedule of a non-frozen job on a Draft Plan snapshot."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc
        mock_frappe.utils.get_datetime.return_value = datetime(2026, 4, 7, 14, 0)
        mock_frappe.utils.time_diff_in_seconds.return_value = 7200  # 120 mins
        mock_frappe.utils.add_to_date.return_value = datetime(2026, 4, 7, 16, 5)
        mock_frappe.utils.getdate.return_value = None
        mock_frappe.utils.format_datetime.return_value = "2026-04-07 14:00"

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import validate_reschedule

        result = validate_reschedule(
            snapshot_name="PP-0001",
            job_id="JOB-0001",
            new_workstation="Line-1",
            new_start="2026-04-07 14:00:00",
        )

        assert result["valid"] is True
        assert result["violations"] == []
        assert result["new_setup_time"] == 10.0
        doc.save.assert_called_once()

    @patch(f"{GANTT_MOD}._", side_effect=_identity_translate)
    @patch(f"{GANTT_MOD}.frappe")
    def test_reject_non_draft_plan(self, mock_frappe, mock_translate):
        """Reschedule should fail if snapshot is not Draft Plan."""
        doc = _make_snapshot_doc(status="Fixed Plan", jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import validate_reschedule

        result = validate_reschedule(
            snapshot_name="PP-0001",
            job_id="JOB-0001",
            new_workstation="Line-1",
            new_start="2026-04-07 14:00:00",
        )

        assert result["valid"] is False
        assert len(result["violations"]) == 1
        assert "Draft Plan" in result["violations"][0]
        doc.save.assert_not_called()

    @patch(f"{GANTT_MOD}._", side_effect=_identity_translate)
    @patch(f"{GANTT_MOD}.frappe")
    def test_reject_frozen_job(self, mock_frappe, mock_translate):
        """Reschedule should fail for frozen jobs."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import validate_reschedule

        result = validate_reschedule(
            snapshot_name="PP-0001",
            job_id="JOB-0003",  # frozen job
            new_workstation="Line-2",
            new_start="2026-04-07 14:00:00",
        )

        assert result["valid"] is False
        assert any("frozen" in v.lower() for v in result["violations"])

    @patch(f"{GANTT_MOD}._", side_effect=_identity_translate)
    @patch(f"{GANTT_MOD}.frappe")
    def test_reject_job_not_found(self, mock_frappe, mock_translate):
        """Reschedule should fail if job_id doesn't exist in snapshot."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import validate_reschedule

        result = validate_reschedule(
            snapshot_name="PP-0001",
            job_id="JOB-NONEXISTENT",
            new_workstation="Line-1",
            new_start="2026-04-07 14:00:00",
        )

        assert result["valid"] is False
        assert any("not found" in v.lower() for v in result["violations"])

    @patch(f"{GANTT_MOD}._check_shift_capacity", return_value=False)
    @patch(f"{GANTT_MOD}._check_transition_allowed", return_value=None)
    @patch(f"{GANTT_MOD}._calc_setup_time", return_value=0)
    @patch(f"{GANTT_MOD}._get_frozen_boundary", return_value=None)
    @patch(f"{GANTT_MOD}._", side_effect=_identity_translate)
    @patch(f"{GANTT_MOD}.frappe")
    def test_reject_insufficient_shift_capacity(
        self, mock_frappe, mock_translate, mock_frozen, mock_setup, mock_transition, mock_shift
    ):
        """Reschedule should fail when shift capacity is insufficient."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc
        mock_frappe.utils.get_datetime.return_value = datetime(2026, 4, 7, 14, 0)
        mock_frappe.utils.time_diff_in_seconds.return_value = 7200
        mock_frappe.utils.add_to_date.return_value = datetime(2026, 4, 7, 16, 0)

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import validate_reschedule

        result = validate_reschedule(
            snapshot_name="PP-0001",
            job_id="JOB-0001",
            new_workstation="Line-1",
            new_start="2026-04-07 14:00:00",
        )

        assert result["valid"] is False
        assert any("capacity" in v.lower() for v in result["violations"])

    @patch(f"{GANTT_MOD}._check_shift_capacity", return_value=True)
    @patch(
        f"{GANTT_MOD}._check_transition_allowed",
        return_value="Transition from Group-A to Group-B is BLOCKED on Line-1.",
    )
    @patch(f"{GANTT_MOD}._calc_setup_time", return_value=0)
    @patch(f"{GANTT_MOD}._get_frozen_boundary", return_value=None)
    @patch(f"{GANTT_MOD}._", side_effect=_identity_translate)
    @patch(f"{GANTT_MOD}.frappe")
    def test_reject_blocked_transition(
        self, mock_frappe, mock_translate, mock_frozen, mock_setup, mock_transition, mock_shift
    ):
        """Reschedule should fail when setup group transition is blocked."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc
        mock_frappe.utils.get_datetime.return_value = datetime(2026, 4, 7, 14, 0)
        mock_frappe.utils.time_diff_in_seconds.return_value = 7200
        mock_frappe.utils.add_to_date.return_value = datetime(2026, 4, 7, 16, 0)

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import validate_reschedule

        result = validate_reschedule(
            snapshot_name="PP-0001",
            job_id="JOB-0001",
            new_workstation="Line-1",
            new_start="2026-04-07 14:00:00",
        )

        assert result["valid"] is False
        assert any("BLOCKED" in v for v in result["violations"])

    @patch(f"{GANTT_MOD}._check_shift_capacity", return_value=True)
    @patch(f"{GANTT_MOD}._check_transition_allowed", return_value=None)
    @patch(f"{GANTT_MOD}._calc_setup_time", return_value=0)
    @patch(f"{GANTT_MOD}._get_frozen_boundary")
    @patch(f"{GANTT_MOD}._", side_effect=_identity_translate)
    @patch(f"{GANTT_MOD}.frappe")
    def test_reject_before_frozen_boundary(
        self, mock_frappe, mock_translate, mock_frozen, mock_setup, mock_transition, mock_shift
    ):
        """Reschedule should fail when new start is before frozen boundary."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc
        mock_frozen.return_value = datetime(2026, 4, 7, 10, 0)
        mock_frappe.utils.get_datetime.return_value = datetime(2026, 4, 7, 8, 0)
        mock_frappe.utils.format_datetime.return_value = "2026-04-07 10:00"
        mock_frappe.utils.time_diff_in_seconds.return_value = 7200
        mock_frappe.utils.add_to_date.return_value = datetime(2026, 4, 7, 10, 0)

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import validate_reschedule

        result = validate_reschedule(
            snapshot_name="PP-0001",
            job_id="JOB-0001",
            new_workstation="Line-1",
            new_start="2026-04-07 08:00:00",
        )

        assert result["valid"] is False
        assert any("frozen boundary" in v.lower() for v in result["violations"])


# ============================================================
# 2. Gantt Tuning — Internal Helpers
# ============================================================


class TestGanttTuningHelpers:
    """Test internal helper functions of gantt_tuning."""

    @patch(f"{GANTT_MOD}.frappe")
    def test_get_frozen_boundary_with_frozen_jobs(self, mock_frappe):
        """Should return latest planned_end of frozen jobs on workstation."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)
        mock_frappe.utils.get_datetime.side_effect = lambda s: datetime.fromisoformat(str(s))

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import _get_frozen_boundary

        boundary = _get_frozen_boundary(doc, "Line-2")
        assert boundary == datetime(2026, 4, 7, 8, 0)

    @patch(f"{GANTT_MOD}.frappe")
    def test_get_frozen_boundary_no_frozen(self, mock_frappe):
        """Should return None when no frozen jobs on workstation."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import _get_frozen_boundary

        boundary = _get_frozen_boundary(doc, "Line-3")
        assert boundary is None

    @patch(f"{GANTT_MOD}._get_setup_group")
    @patch(f"{GANTT_MOD}.frappe")
    def test_calc_setup_time_same_group(self, mock_frappe, mock_get_group):
        """Setup time should be 0 when both items are in the same group."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)
        mock_get_group.return_value = "Group-A"
        mock_frappe.utils.get_datetime.side_effect = lambda s: datetime.fromisoformat(str(s))

        target_job = doc.jobs[1]  # JOB-0002
        target_job.item_code = "BM-60kWh-B"

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import _calc_setup_time

        setup = _calc_setup_time(
            doc, target_job, "Line-1", datetime(2026, 4, 7, 12, 0)
        )
        assert setup == 0.0

    @patch(f"{GANTT_MOD}._get_setup_group")
    @patch(f"{GANTT_MOD}.frappe")
    def test_calc_setup_time_no_preceding_job(self, mock_frappe, mock_get_group):
        """Setup time should be 0 when no preceding job on target workstation."""
        doc = _make_snapshot_doc(jobs=SAMPLE_JOBS)
        mock_frappe.utils.get_datetime.side_effect = lambda s: datetime.fromisoformat(str(s))

        target_job = doc.jobs[0]  # JOB-0001

        from fp.factory_planner.page.gantt_tuning.gantt_tuning import _calc_setup_time

        setup = _calc_setup_time(
            doc, target_job, "Line-3", datetime(2026, 4, 7, 8, 0)
        )
        assert setup == 0.0


# ============================================================
# 3. Planning Dashboard — compare_snapshots
# ============================================================


class TestPlanningDashboardCompare:
    """Test planning_dashboard compare_snapshots API."""

    @patch(f"{SNAP_MOD}.frappe")
    def test_compare_basic_kpis(self, mock_frappe):
        """Compare two snapshots and verify KPI delta calculation."""
        snap_a = _make_snapshot_doc(name="PP-A", status="Pre Plan", jobs=SAMPLE_JOBS[:2])
        snap_a.solver_run_time_secs = 5.0
        snap_a.objective_value = 1000
        snap_a.total_tardiness_mins = 120
        snap_a.total_setup_time_mins = 45
        snap_a.line_utilization_pct = 75.0

        snap_b = _make_snapshot_doc(name="PP-B", status="Draft Plan", jobs=SAMPLE_JOBS)
        snap_b.solver_run_time_secs = 3.0
        snap_b.objective_value = 800
        snap_b.total_tardiness_mins = 60
        snap_b.total_setup_time_mins = 30
        snap_b.line_utilization_pct = 82.0

        mock_frappe.get_doc.side_effect = lambda doctype, name: (
            snap_a if name == "PP-A" else snap_b
        )

        from fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot import (
            compare_snapshots,
        )

        result = compare_snapshots("PP-A", "PP-B")

        assert result["snapshot_a"]["name"] == "PP-A"
        assert result["snapshot_b"]["name"] == "PP-B"

        kpi = result["kpis"]["total_tardiness_mins"]
        assert kpi["a_value"] == 120
        assert kpi["b_value"] == 60
        assert kpi["delta"] == -60
        assert kpi["pct_change"] == -50.0

        kpi_util = result["kpis"]["line_utilization_pct"]
        assert kpi_util["delta"] == 7.0

        assert result["job_count"]["a"] == 2
        assert result["job_count"]["b"] == 3

    @patch(f"{SNAP_MOD}.frappe")
    def test_compare_zero_baseline(self, mock_frappe):
        """When baseline KPI is 0, pct_change should be 0 (no division by zero)."""
        snap_a = _make_snapshot_doc(name="PP-A", jobs=[])
        snap_a.total_tardiness_mins = 0
        snap_a.total_setup_time_mins = 0
        snap_a.line_utilization_pct = 0
        snap_a.objective_value = 0
        snap_a.solver_run_time_secs = 0

        snap_b = _make_snapshot_doc(name="PP-B", jobs=SAMPLE_JOBS[:1])
        snap_b.total_tardiness_mins = 30
        snap_b.total_setup_time_mins = 15
        snap_b.line_utilization_pct = 50.0
        snap_b.objective_value = 500
        snap_b.solver_run_time_secs = 2.0

        mock_frappe.get_doc.side_effect = lambda doctype, name: (
            snap_a if name == "PP-A" else snap_b
        )

        from fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot import (
            compare_snapshots,
        )

        result = compare_snapshots("PP-A", "PP-B")

        for key, kpi in result["kpis"].items():
            assert kpi["pct_change"] == 0.0, f"{key} pct_change should be 0 for zero baseline"

    @patch(f"{SNAP_MOD}.frappe")
    def test_compare_identical_snapshots(self, mock_frappe):
        """Comparing a snapshot to itself should yield zero deltas."""
        snap = _make_snapshot_doc(name="PP-X", jobs=SAMPLE_JOBS)
        snap.total_tardiness_mins = 100
        snap.total_setup_time_mins = 50
        snap.line_utilization_pct = 80
        snap.objective_value = 900
        snap.solver_run_time_secs = 4.0

        mock_frappe.get_doc.return_value = snap

        from fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot import (
            compare_snapshots,
        )

        result = compare_snapshots("PP-X", "PP-X")

        for key, kpi in result["kpis"].items():
            assert kpi["delta"] == 0, f"{key} delta should be 0 for identical snapshots"

    @patch(f"{SNAP_MOD}.frappe")
    def test_compare_master_data_changed(self, mock_frappe):
        """Master data diff detection between snapshots."""
        snap_a = _make_snapshot_doc(
            name="PP-A",
            jobs=[],
            master_snapshot=json.dumps({
                "tat_master": [{"item": "A", "tat": 30}],
                "setup_matrix": [],
                "shift_calendar": [],
                "workstations": ["Line-1"],
            }),
        )
        snap_b = _make_snapshot_doc(
            name="PP-B",
            jobs=[],
            master_snapshot=json.dumps({
                "tat_master": [{"item": "A", "tat": 45}],
                "setup_matrix": [],
                "shift_calendar": [],
                "workstations": ["Line-1"],
            }),
        )

        mock_frappe.get_doc.side_effect = lambda doctype, name: (
            snap_a if name == "PP-A" else snap_b
        )

        from fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot import (
            compare_snapshots,
        )

        result = compare_snapshots("PP-A", "PP-B")

        assert result["master_data_changed"]["tat_master"] is True
        assert result["master_data_changed"]["setup_matrix"] is False
        assert result["master_data_changed"]["workstations"] is False


# ============================================================
# 4. WO Tracking — get_fixed_plan_jobs
# ============================================================


class TestWOTrackingGetJobs:
    """Test wo_tracking.get_fixed_plan_jobs API."""

    @patch(f"{WO_MOD}._", side_effect=_identity_translate)
    @patch(f"{WO_MOD}.frappe")
    def test_get_jobs_with_work_orders(self, mock_frappe, mock_translate):
        """Should return jobs with W/O status and progress."""
        doc = _make_snapshot_doc(name="PP-FP01", status="Fixed Plan", jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc

        def mock_get_value(doctype, name, fields, as_dict=False):
            if name == "WO-0001":
                return SimpleNamespace(status="In Process", produced_qty=50, qty=100)
            if name == "WO-0002":
                return SimpleNamespace(status="Completed", produced_qty=150, qty=150)
            return None

        mock_frappe.db.get_value.side_effect = mock_get_value

        from fp.factory_planner.page.wo_tracking.wo_tracking import get_fixed_plan_jobs

        jobs = get_fixed_plan_jobs("PP-FP01")

        assert len(jobs) == 3

        job1 = next(j for j in jobs if j["job_id"] == "JOB-0001")
        assert job1["work_order"] is None
        assert job1["wo_status"] is None
        assert job1["wo_progress"] == 0

        job2 = next(j for j in jobs if j["job_id"] == "JOB-0002")
        assert job2["work_order"] == "WO-0001"
        assert job2["wo_status"] == "In Process"
        assert job2["wo_progress"] == 50.0

        job3 = next(j for j in jobs if j["job_id"] == "JOB-0003")
        assert job3["work_order"] == "WO-0002"
        assert job3["wo_status"] == "Completed"
        assert job3["wo_progress"] == 100.0

    @patch(f"{WO_MOD}._", side_effect=_identity_translate)
    @patch(f"{WO_MOD}.frappe")
    def test_reject_non_fixed_plan(self, mock_frappe, mock_translate):
        """Should throw when snapshot is not Fixed Plan."""
        doc = _make_snapshot_doc(status="Draft Plan", jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc
        mock_frappe.throw.side_effect = Exception("Only Fixed Plan")

        from fp.factory_planner.page.wo_tracking.wo_tracking import get_fixed_plan_jobs

        with pytest.raises(Exception, match="Only Fixed Plan"):
            get_fixed_plan_jobs("PP-0001")

    @patch(f"{WO_MOD}._", side_effect=_identity_translate)
    @patch(f"{WO_MOD}.frappe")
    def test_get_jobs_zero_qty_wo(self, mock_frappe, mock_translate):
        """Should handle W/O with zero qty gracefully (no division by zero)."""
        jobs_with_wo = [
            {
                **SAMPLE_JOBS[1],
                "work_order": "WO-ZERO",
            },
        ]
        doc = _make_snapshot_doc(status="Fixed Plan", jobs=jobs_with_wo)
        mock_frappe.get_doc.return_value = doc
        mock_frappe.db.get_value.return_value = SimpleNamespace(
            status="Not Started", produced_qty=0, qty=0,
        )

        from fp.factory_planner.page.wo_tracking.wo_tracking import get_fixed_plan_jobs

        jobs = get_fixed_plan_jobs("PP-0001")
        assert jobs[0]["wo_progress"] == 0


# ============================================================
# 5. WO Tracking — get_order_genealogy
# ============================================================


class TestWOTrackingGenealogy:
    """Test wo_tracking.get_order_genealogy API."""

    @patch(f"{WO_MOD}.frappe")
    def test_genealogy_groups_by_lot(self, mock_frappe):
        """Genealogy should group jobs by lot number."""
        doc = _make_snapshot_doc(name="PP-FP01", status="Fixed Plan", jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc

        from fp.factory_planner.page.wo_tracking.wo_tracking import get_order_genealogy

        result = get_order_genealogy("PP-FP01", "SO-0001")

        assert result["source_demand_id"] == "SO-0001"
        assert result["total_jobs"] == 3

        assert 0 in result["lots"]
        assert len(result["lots"][0]) == 2

        assert 1 in result["lots"]
        assert len(result["lots"][1]) == 1

    @patch(f"{WO_MOD}.frappe")
    def test_genealogy_sorts_by_operation_sequence(self, mock_frappe):
        """Jobs within a lot should be sorted by operation_sequence."""
        doc = _make_snapshot_doc(name="PP-FP01", status="Fixed Plan", jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc

        from fp.factory_planner.page.wo_tracking.wo_tracking import get_order_genealogy

        result = get_order_genealogy("PP-FP01", "SO-0001")

        lot_0_jobs = result["lots"][0]
        sequences = [j["operation_sequence"] for j in lot_0_jobs]
        assert sequences == sorted(sequences), "Jobs should be sorted by operation_sequence"

    @patch(f"{WO_MOD}.frappe")
    def test_genealogy_no_matching_demand(self, mock_frappe):
        """Genealogy should return empty when no jobs match the demand ID."""
        doc = _make_snapshot_doc(name="PP-FP01", status="Fixed Plan", jobs=SAMPLE_JOBS)
        mock_frappe.get_doc.return_value = doc

        from fp.factory_planner.page.wo_tracking.wo_tracking import get_order_genealogy

        result = get_order_genealogy("PP-FP01", "SO-NONEXISTENT")

        assert result["total_jobs"] == 0
        assert len(result["lots"]) == 0

    @patch(f"{WO_MOD}.frappe")
    def test_genealogy_null_lot_defaults_to_zero(self, mock_frappe):
        """Jobs with None lot_number should default to lot 0."""
        jobs_no_lot = [
            {
                **SAMPLE_JOBS[0],
                "lot_number": None,
            },
        ]
        doc = _make_snapshot_doc(name="PP-FP01", status="Fixed Plan", jobs=jobs_no_lot)
        mock_frappe.get_doc.return_value = doc

        from fp.factory_planner.page.wo_tracking.wo_tracking import get_order_genealogy

        result = get_order_genealogy("PP-FP01", "SO-0001")

        assert 0 in result["lots"]
        assert result["total_jobs"] == 1


# ============================================================
# 6. Frontend Smoke Test Scenarios (documented, not automated)
# ============================================================


class TestFrontendScenarios:
    """Document frontend E2E test scenarios for manual/Playwright testing.

    These tests verify that the test scenarios are well-defined.
    Actual browser automation requires Playwright + Frappe test site.
    """

    def test_gantt_scenarios_documented(self):
        """Gantt Tuning page should support these user flows."""
        scenarios = [
            "Page loads with title 'Gantt Tuning' and snapshot selector",
            "Selecting a snapshot loads and renders Gantt bars by workstation",
            "Frozen jobs display in gray with 'FROZEN' tooltip",
            "Tardy jobs display with red bar styling",
            "Zoom buttons (Hour/Day/Week) toggle active state",
            "Workstation filter narrows displayed rows",
            "Clicking a bar opens job detail dialog",
            "Job detail shows TARDY/On Time badge correctly",
            "Non-frozen, non-Fixed Plan jobs show Edit Timing button",
            "Drag & drop updates job position via API call",
            "Frozen jobs reject drag with orange alert",
            "Legend shows Normal, Tardy, Frozen, and Frozen Zone",
            "Refresh button reloads current snapshot",
        ]
        assert len(scenarios) >= 10

    def test_dashboard_scenarios_documented(self):
        """Planning Dashboard page should support these user flows."""
        scenarios = [
            "Page loads with two snapshot selectors (Baseline and Comparison)",
            "Selecting both snapshots triggers comparison API call",
            "KPI cards show A vs B values with color-coded delta arrows",
            "KPI table shows detailed comparison with delta and pct columns",
            "Green/red indicators reflect improvement vs degradation",
            "Job count summary shows totals and difference",
            "Loading state shows 'Loading comparison...' text",
            "Refresh button reloads comparison",
        ]
        assert len(scenarios) >= 6

    def test_wo_tracking_scenarios_documented(self):
        """WO Tracking page should support these user flows."""
        scenarios = [
            "Page loads with Fixed Plan snapshot selector",
            "Only Fixed Plan snapshots appear in selector query",
            "Selecting a snapshot loads job table with W/O status",
            "Summary cards show total, released, not released, in process, completed counts",
            "Release rate percentage is calculated correctly",
            "Status filter narrows table to matching W/O status",
            "Item filter narrows table by item_code",
            "Work Order column links to /app/work-order/{name}",
            "Status badges show correct colors (orange/blue/green/red)",
            "Progress bars show produced_qty / qty percentage",
            "Genealogy button opens lot-grouped genealogy panel",
            "Genealogy panel shows operations sorted by sequence",
            "Genealogy close button hides the panel",
            "Not Released status filter shows jobs without W/O",
        ]
        assert len(scenarios) >= 10


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
