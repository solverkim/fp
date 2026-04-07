"""Tests for Frozen Window release and Daily Split logic.

Unit tests using mocks for Frappe DB operations since these run
outside a Frappe bench environment.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from fp.frozen_window.daily_split import (
	create_child_job,
	get_completed_work_orders_with_shortfall,
	process_daily_split,
)
from fp.frozen_window.release import (
	create_work_order_from_job,
	get_active_fixed_plan,
	get_default_bom,
	get_frozen_jobs,
	mark_job_frozen,
	release_frozen_window_orders,
)


# ============================================================
# Fixtures & Helpers
# ============================================================


def make_snapshot_job(
	name: str = "row-001",
	job_id: str = "JOB-0001",
	item_code: str = "BM-60kWh-A",
	qty: float = 200,
	workstation: str = "Line-1",
	operation: str = "Module Assembly",
	operation_sequence: int = 10,
	planned_start: datetime | None = None,
	planned_end: datetime | None = None,
	due_date: date | None = None,
	is_frozen: int = 0,
	work_order: str | None = None,
) -> SimpleNamespace:
	"""Create a mock FP Snapshot Job child row."""
	base_date = date(2026, 4, 10)
	return SimpleNamespace(
		name=name,
		job_id=job_id,
		item_code=item_code,
		qty=qty,
		workstation=workstation,
		operation=operation,
		operation_sequence=operation_sequence,
		planned_start=planned_start or datetime(base_date.year, base_date.month, base_date.day, 8, 0),
		planned_end=planned_end or datetime(base_date.year, base_date.month, base_date.day, 12, 0),
		due_date=due_date or base_date,
		is_frozen=is_frozen,
		work_order=work_order,
	)


def make_work_order(
	name: str = "WO-0001",
	production_item: str = "BM-60kWh-A",
	qty: float = 200,
	produced_qty: float = 150,
	expected_delivery_date: date | None = None,
	custom_fp_snapshot_job: str = "row-001",
) -> SimpleNamespace:
	return SimpleNamespace(
		name=name,
		production_item=production_item,
		qty=qty,
		produced_qty=produced_qty,
		expected_delivery_date=expected_delivery_date or date(2026, 4, 14),
		custom_fp_snapshot_job=custom_fp_snapshot_job,
	)


# ============================================================
# Module 1: Frozen Window Release Tests
# ============================================================


class TestCreateChildJob:
	"""Test create_child_job (pure function, no mocks needed)."""

	def test_basic_child_creation(self):
		child = create_child_job("JOB-0001", 200, "2026-04-14")
		assert child["qty"] == 200
		assert child["priority"] == "critical"
		assert "SPLIT" in child["job_id"]
		assert child["parent_job_id"] == "JOB-0001"

	def test_child_inherits_due_date(self):
		child = create_child_job("JOB-0002", 50, date(2026, 4, 18))
		assert child["due_date"] == date(2026, 4, 18)

	def test_child_zero_qty(self):
		child = create_child_job("JOB-0003", 0, "2026-04-14")
		assert child["qty"] == 0

	def test_child_fractional_qty(self):
		child = create_child_job("JOB-0004", 33.5, "2026-04-14")
		assert child["qty"] == 33.5


def _real_getdate(d):
	"""Minimal getdate that handles datetime/date/str without Frappe internals."""
	if isinstance(d, datetime):
		return d
	if isinstance(d, date):
		return d
	if isinstance(d, str):
		return datetime.strptime(d, "%Y-%m-%d").date()
	return d


class TestGetFrozenJobs:
	"""Test get_frozen_jobs filtering logic."""

	@patch("fp.frozen_window.release.frappe")
	def test_filters_by_target_date(self, mock_frappe):
		mock_frappe.utils.getdate = _real_getdate
		target_date = date(2026, 4, 10)
		job_match = make_snapshot_job(
			name="row-001",
			planned_start=datetime(2026, 4, 10, 8, 0),
		)
		job_wrong_date = make_snapshot_job(
			name="row-002",
			planned_start=datetime(2026, 4, 11, 8, 0),
		)

		mock_snapshot = SimpleNamespace(jobs=[job_match, job_wrong_date])
		mock_frappe.get_doc.return_value = mock_snapshot

		result = get_frozen_jobs("PP-0001", target_date)

		assert len(result) == 1
		assert result[0].name == "row-001"

	@patch("fp.frozen_window.release.frappe")
	def test_excludes_already_frozen(self, mock_frappe):
		mock_frappe.utils.getdate = _real_getdate
		target_date = date(2026, 4, 10)
		frozen_job = make_snapshot_job(
			name="row-001",
			planned_start=datetime(2026, 4, 10, 8, 0),
			is_frozen=1,
		)

		mock_snapshot = SimpleNamespace(jobs=[frozen_job])
		mock_frappe.get_doc.return_value = mock_snapshot

		result = get_frozen_jobs("PP-0001", target_date)
		assert len(result) == 0

	@patch("fp.frozen_window.release.frappe")
	def test_excludes_already_linked_to_wo(self, mock_frappe):
		mock_frappe.utils.getdate = _real_getdate
		target_date = date(2026, 4, 10)
		linked_job = make_snapshot_job(
			name="row-001",
			planned_start=datetime(2026, 4, 10, 8, 0),
			work_order="WO-0001",
		)

		mock_snapshot = SimpleNamespace(jobs=[linked_job])
		mock_frappe.get_doc.return_value = mock_snapshot

		result = get_frozen_jobs("PP-0001", target_date)
		assert len(result) == 0


class TestGetActiveFixedPlan:
	"""Test get_active_fixed_plan snapshot lookup."""

	@patch("fp.frozen_window.release.frappe")
	def test_returns_fixed_plan(self, mock_frappe):
		mock_frappe.db.get_value.return_value = "PP-0001"
		mock_frappe.get_doc.return_value = SimpleNamespace(name="PP-0001", status="Fixed Plan")

		result = get_active_fixed_plan()

		assert result.name == "PP-0001"
		mock_frappe.db.get_value.assert_called_once()

	@patch("fp.frozen_window.release.frappe")
	def test_returns_none_when_no_fixed_plan(self, mock_frappe):
		mock_frappe.db.get_value.return_value = None

		result = get_active_fixed_plan()
		assert result is None


class TestGetDefaultBom:
	"""Test BOM lookup."""

	@patch("fp.frozen_window.release.frappe")
	def test_returns_bom_name(self, mock_frappe):
		mock_frappe.db.get_value.return_value = "BOM-BM-60kWh-A-001"

		result = get_default_bom("BM-60kWh-A")
		assert result == "BOM-BM-60kWh-A-001"

	@patch("fp.frozen_window.release.frappe")
	def test_returns_none_when_no_bom(self, mock_frappe):
		mock_frappe.db.get_value.return_value = None

		result = get_default_bom("MISSING-ITEM")
		assert result is None


class TestCreateWorkOrderFromJob:
	"""Test Work Order creation from snapshot job."""

	@patch("fp.frozen_window.release.get_default_bom")
	@patch("fp.frozen_window.release.frappe")
	def test_creates_work_order(self, mock_frappe, mock_get_bom):
		mock_get_bom.return_value = "BOM-BM-60kWh-A-001"

		mock_wo = MagicMock()
		mock_wo.name = "WO-0001"
		mock_frappe.new_doc.return_value = mock_wo
		mock_frappe.defaults.get_defaults.return_value = {"company": "Test Co"}

		job = make_snapshot_job()
		result = create_work_order_from_job(job)

		assert result == "WO-0001"
		mock_wo.insert.assert_called_once()
		mock_wo.submit.assert_called_once()
		assert mock_wo.production_item == "BM-60kWh-A"
		assert mock_wo.qty == 200
		assert mock_wo.bom_no == "BOM-BM-60kWh-A-001"

	@patch("fp.frozen_window.release.get_default_bom")
	@patch("fp.frozen_window.release.frappe")
	def test_returns_none_without_bom(self, mock_frappe, mock_get_bom):
		mock_get_bom.return_value = None

		job = make_snapshot_job()
		result = create_work_order_from_job(job)

		assert result is None
		mock_frappe.new_doc.assert_not_called()


class TestMarkJobFrozen:
	"""Test marking snapshot jobs as frozen."""

	@patch("fp.frozen_window.release.frappe")
	def test_sets_frozen_and_work_order(self, mock_frappe):
		mark_job_frozen("PP-0001", "row-001", "WO-0001")

		mock_frappe.db.set_value.assert_called_once_with(
			"FP Snapshot Job",
			"row-001",
			{"is_frozen": 1, "work_order": "WO-0001"},
		)


class TestReleaseFrozenWindowOrders:
	"""Test the main release orchestrator."""

	def _setup_frappe_utils(self, mock_frappe):
		mock_frappe.utils.today.return_value = "2026-04-08"
		mock_frappe.utils.getdate = _real_getdate

	@patch("fp.frozen_window.release.mark_job_frozen")
	@patch("fp.frozen_window.release.create_work_order_from_job")
	@patch("fp.frozen_window.release.get_frozen_jobs")
	@patch("fp.frozen_window.release.get_active_fixed_plan")
	@patch("fp.frozen_window.release.frappe")
	def test_full_release_flow(
		self, mock_frappe, mock_get_plan, mock_get_jobs, mock_create_wo, mock_mark
	):
		self._setup_frappe_utils(mock_frappe)
		mock_get_plan.return_value = SimpleNamespace(name="PP-0001")
		job = make_snapshot_job()
		mock_get_jobs.return_value = [job]
		mock_create_wo.return_value = "WO-0001"

		result = release_frozen_window_orders()

		assert result == ["WO-0001"]
		mock_create_wo.assert_called_once_with(job)
		mock_mark.assert_called_once_with("PP-0001", "row-001", "WO-0001")

	@patch("fp.frozen_window.release.get_active_fixed_plan")
	@patch("fp.frozen_window.release.frappe")
	def test_no_fixed_plan_returns_empty(self, mock_frappe, mock_get_plan):
		self._setup_frappe_utils(mock_frappe)
		mock_get_plan.return_value = None

		result = release_frozen_window_orders()
		assert result == []

	@patch("fp.frozen_window.release.get_frozen_jobs")
	@patch("fp.frozen_window.release.get_active_fixed_plan")
	@patch("fp.frozen_window.release.frappe")
	def test_no_jobs_returns_empty(self, mock_frappe, mock_get_plan, mock_get_jobs):
		self._setup_frappe_utils(mock_frappe)
		mock_get_plan.return_value = SimpleNamespace(name="PP-0001")
		mock_get_jobs.return_value = []

		result = release_frozen_window_orders()
		assert result == []

	@patch("fp.frozen_window.release.mark_job_frozen")
	@patch("fp.frozen_window.release.create_work_order_from_job")
	@patch("fp.frozen_window.release.get_frozen_jobs")
	@patch("fp.frozen_window.release.get_active_fixed_plan")
	@patch("fp.frozen_window.release.frappe")
	def test_skips_already_frozen_jobs(
		self, mock_frappe, mock_get_plan, mock_get_jobs, mock_create_wo, mock_mark
	):
		self._setup_frappe_utils(mock_frappe)
		mock_get_plan.return_value = SimpleNamespace(name="PP-0001")
		frozen_job = make_snapshot_job(is_frozen=1)
		mock_get_jobs.return_value = [frozen_job]

		result = release_frozen_window_orders()

		assert result == []
		mock_create_wo.assert_not_called()

	@patch("fp.frozen_window.release.mark_job_frozen")
	@patch("fp.frozen_window.release.create_work_order_from_job")
	@patch("fp.frozen_window.release.get_frozen_jobs")
	@patch("fp.frozen_window.release.get_active_fixed_plan")
	@patch("fp.frozen_window.release.frappe")
	def test_skips_jobs_with_existing_wo(
		self, mock_frappe, mock_get_plan, mock_get_jobs, mock_create_wo, mock_mark
	):
		self._setup_frappe_utils(mock_frappe)
		mock_get_plan.return_value = SimpleNamespace(name="PP-0001")
		linked_job = make_snapshot_job(work_order="WO-existing")
		mock_get_jobs.return_value = [linked_job]

		result = release_frozen_window_orders()

		assert result == []
		mock_create_wo.assert_not_called()


# ============================================================
# Module 2: Daily Split Tests
# ============================================================


class TestGetCompletedWorkOrdersWithShortfall:
	"""Test Work Order shortfall query."""

	@patch("fp.frozen_window.daily_split.frappe")
	def test_filters_shortfall_orders(self, mock_frappe):
		wo_full = SimpleNamespace(
			name="WO-0001", production_item="A", qty=200, produced_qty=200,
			expected_delivery_date=date(2026, 4, 14), custom_fp_snapshot_job="row-001",
		)
		wo_short = SimpleNamespace(
			name="WO-0002", production_item="B", qty=200, produced_qty=150,
			expected_delivery_date=date(2026, 4, 14), custom_fp_snapshot_job="row-002",
		)
		mock_frappe.get_all.return_value = [wo_full, wo_short]

		result = get_completed_work_orders_with_shortfall(date(2026, 4, 10))

		assert len(result) == 1
		assert result[0].name == "WO-0002"


class TestProcessDailySplit:
	"""Test the main daily split orchestrator."""

	def _setup_frappe_utils(self, mock_frappe):
		mock_frappe.utils.today.return_value = "2026-04-08"
		mock_frappe.utils.getdate = _real_getdate

	@patch("fp.frozen_window.daily_split.add_to_demand_pool")
	@patch("fp.frozen_window.daily_split.get_completed_work_orders_with_shortfall")
	@patch("fp.frozen_window.daily_split.frappe")
	def test_creates_children_for_shortfall(self, mock_frappe, mock_get_wos, mock_add):
		self._setup_frappe_utils(mock_frappe)
		wo = make_work_order(produced_qty=150)  # 50 shortfall
		mock_get_wos.return_value = [wo]
		mock_add.return_value = "snapshot-row-new"

		result = process_daily_split()

		assert len(result) == 1
		assert result[0]["qty"] == 50
		assert result[0]["priority"] == "critical"
		mock_add.assert_called_once()

	@patch("fp.frozen_window.daily_split.get_completed_work_orders_with_shortfall")
	@patch("fp.frozen_window.daily_split.frappe")
	def test_no_shortfall_returns_empty(self, mock_frappe, mock_get_wos):
		self._setup_frappe_utils(mock_frappe)
		mock_get_wos.return_value = []

		result = process_daily_split()
		assert result == []

	@patch("fp.frozen_window.daily_split.add_to_demand_pool")
	@patch("fp.frozen_window.daily_split.get_completed_work_orders_with_shortfall")
	@patch("fp.frozen_window.daily_split.frappe")
	def test_skips_fully_produced(self, mock_frappe, mock_get_wos, mock_add):
		self._setup_frappe_utils(mock_frappe)
		wo = make_work_order(qty=200, produced_qty=200)
		mock_get_wos.return_value = [wo]

		result = process_daily_split()

		assert result == []
		mock_add.assert_not_called()

	@patch("fp.frozen_window.daily_split.add_to_demand_pool")
	@patch("fp.frozen_window.daily_split.get_completed_work_orders_with_shortfall")
	@patch("fp.frozen_window.daily_split.frappe")
	def test_handles_multiple_shortfalls(self, mock_frappe, mock_get_wos, mock_add):
		self._setup_frappe_utils(mock_frappe)
		wo1 = make_work_order(name="WO-1", qty=200, produced_qty=100, custom_fp_snapshot_job="row-001")
		wo2 = make_work_order(name="WO-2", qty=300, produced_qty=250, custom_fp_snapshot_job="row-002")
		mock_get_wos.return_value = [wo1, wo2]
		mock_add.side_effect = ["snap-row-1", "snap-row-2"]

		result = process_daily_split()

		assert len(result) == 2
		assert result[0]["qty"] == 100
		assert result[1]["qty"] == 50


class TestAddToDemandPool:
	"""Test demand pool insertion."""

	@patch("fp.frozen_window.daily_split.frappe")
	def test_adds_to_pre_plan_snapshot(self, mock_frappe):
		from fp.frozen_window.daily_split import add_to_demand_pool

		mock_frappe.db.get_value.return_value = "PP-0005"

		mock_row = SimpleNamespace(name="new-row-001")
		mock_snapshot = MagicMock()
		mock_snapshot.append.return_value = mock_row
		mock_frappe.get_doc.return_value = mock_snapshot

		child = {"job_id": "JOB-001-SPLIT", "parent_job_id": "JOB-001", "qty": 50, "due_date": "2026-04-14"}
		result = add_to_demand_pool(child, "BM-60kWh-A")

		assert result == "new-row-001"
		mock_snapshot.append.assert_called_once()
		mock_snapshot.save.assert_called_once()

	@patch("fp.frozen_window.daily_split.frappe")
	def test_returns_none_without_pre_plan(self, mock_frappe):
		from fp.frozen_window.daily_split import add_to_demand_pool

		mock_frappe.db.get_value.return_value = None

		child = {"job_id": "JOB-001-SPLIT", "parent_job_id": "JOB-001", "qty": 50, "due_date": "2026-04-14"}
		result = add_to_demand_pool(child, "BM-60kWh-A")

		assert result is None


# ============================================================
# Integration-style: Frozen Job Exclusion from Solver
# ============================================================


class TestFrozenJobExclusion:
	"""Validate the design principle that frozen jobs are excluded from solver input."""

	def test_frozen_jobs_filtered_out(self):
		all_jobs = [
			{"job_id": "JOB-0001", "is_frozen": True},
			{"job_id": "JOB-0002", "is_frozen": True},
			{"job_id": "JOB-0003", "is_frozen": False},
			{"job_id": "JOB-0004", "is_frozen": False},
		]

		solver_input = [j for j in all_jobs if not j["is_frozen"]]

		assert len(solver_input) == 2
		assert all(not j["is_frozen"] for j in solver_input)

	def test_child_job_has_critical_priority(self):
		child = create_child_job("JOB-0001", 100, "2026-04-14")
		assert child["priority"] == "critical"

	def test_child_job_id_traceable(self):
		child = create_child_job("JOB-0001", 100, "2026-04-14")
		assert child["parent_job_id"] == "JOB-0001"
		assert child["job_id"].startswith("JOB-0001")


if __name__ == "__main__":
	pytest.main([__file__, "-v"])
