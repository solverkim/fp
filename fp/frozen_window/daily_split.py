"""Daily Split — handle unmet production quantities."""


def process_daily_split():
	"""Daily scheduled job: split unmet quantities into child orders.

	This is a stub for the scheduled task. Full implementation in Phase 2.
	"""
	pass


def create_child_job(parent_job_id, remaining_qty, original_due_date):
	"""Create a child job for unmet quantity.

	Args:
		parent_job_id: Original job identifier.
		remaining_qty: Quantity not produced.
		original_due_date: Original due date (child inherits with critical priority).

	Returns dict representing the child job.
	"""
	return {
		"job_id": f"{parent_job_id}-SPLIT",
		"parent_job_id": parent_job_id,
		"qty": remaining_qty,
		"due_date": original_due_date,
		"priority": "critical",
	}
