"""Frozen Window — D+2 Work Order release logic."""


def release_frozen_window_orders():
	"""Daily scheduled job: release D+2 frozen jobs as Work Orders.

	This is a stub for the scheduled task. Full implementation in Phase 2.
	"""
	pass


def get_frozen_jobs(snapshot_name, target_date):
	"""Get jobs from a Fixed Plan snapshot that should be frozen for target_date.

	Args:
		snapshot_name: FP Planning Snapshot name.
		target_date: The D+2 date to freeze.

	Returns list of job dicts.
	"""
	# Phase 2 implementation will query FP Snapshot Job child table
	return []
