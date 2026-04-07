"""Netting algorithm: Gross Demand - Available Inventory - Firm W/O = Net Demand."""

import math


def compute_netting(gross_demand, available_inventory, firm_wo_qty):
	"""Compute net demand after netting.

	Returns net demand (>= 0).
	"""
	net = gross_demand - available_inventory - firm_wo_qty
	return max(0, net)


def split_into_lots(net_demand, lot_size, min_lot_threshold=0.2):
	"""Split net demand into lot-sized jobs.

	Args:
		net_demand: Total quantity to produce.
		lot_size: Standard lot size.
		min_lot_threshold: Fraction of lot_size below which remainder merges
			into the last full lot.

	Returns list of job quantities.
	"""
	if net_demand <= 0 or lot_size <= 0:
		return []

	full_lots = int(net_demand // lot_size)
	remainder = net_demand - (full_lots * lot_size)

	jobs = [lot_size] * full_lots

	if remainder > 0:
		if remainder < lot_size * min_lot_threshold and jobs:
			jobs[-1] += remainder
		else:
			jobs.append(remainder)

	return jobs


def build_demand_profile(demand_items):
	"""Process a list of demand items through netting + lot sizing.

	Args:
		demand_items: list of dicts with keys:
			item_code, gross_demand, available_inventory, firm_wo_qty, lot_size, due_date

	Returns list of job dicts ready for the solver.
	"""
	jobs = []
	job_counter = 0

	for item in demand_items:
		net = compute_netting(
			item["gross_demand"],
			item.get("available_inventory", 0),
			item.get("firm_wo_qty", 0),
		)

		lot_size = item.get("lot_size", 200)
		lot_quantities = split_into_lots(net, lot_size)

		for lot_idx, qty in enumerate(lot_quantities):
			job_counter += 1
			jobs.append({
				"job_id": f"JOB-{job_counter:04d}",
				"item_code": item["item_code"],
				"qty": qty,
				"lot_number": lot_idx + 1,
				"due_date": item["due_date"],
				"source_demand_id": item.get("source_demand_id"),
			})

	return jobs
