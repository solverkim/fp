"""FP Demand Profile — Frappe-integrated demand netting.

Pulls gross demand from Production Plan, queries inventory and firm Work Orders
from ERPNext, computes net demand via netting algorithm, and stores results.

Flow: Production Plan → Gross Demand → (- Inventory - Firm WO) → Net Demand → Lot Split
"""

import frappe
from frappe import _
from frappe.model.document import Document

from fp.demand.netting import compute_netting, split_into_lots


class FPDemandProfile(Document):
	def validate(self) -> None:
		self._compute_netting_for_items()

	def _compute_netting_for_items(self) -> None:
		"""Recalculate net demand and job count for each item row."""
		for item in self.items:
			net = compute_netting(
				item.gross_demand or 0,
				item.available_inventory or 0,
				item.firm_wo_qty or 0,
			)
			item.net_demand = net

			lot_size = item.lot_size or 200
			jobs = split_into_lots(net, lot_size)
			item.num_jobs = len(jobs)


@frappe.whitelist()
def populate_from_production_plan(demand_profile_name: str, production_plan_name: str) -> dict:
	"""Pull demand items from a Production Plan and populate the Demand Profile.

	Queries ERPNext for:
	- Gross demand from Production Plan items (po_items)
	- Available inventory from Bin (actual_qty)
	- Firm Work Order quantities (status in Draft, Not Started, In Process)

	Args:
		demand_profile_name: Name of the FP Demand Profile to populate.
		production_plan_name: Name of the source Production Plan.

	Returns:
		Dict with item_count and total_net_demand summary.
	"""
	profile = frappe.get_doc("FP Demand Profile", demand_profile_name)
	pp = frappe.get_doc("Production Plan", production_plan_name)

	if not pp.po_items:
		frappe.throw(_("Production Plan {0} has no planned items.").format(production_plan_name))

	profile.items = []

	for po_item in pp.po_items:
		item_code = po_item.item_code
		gross_demand = po_item.planned_qty or 0

		available_inventory = _get_available_inventory(item_code, po_item.warehouse)
		firm_wo_qty = _get_firm_wo_qty(item_code)

		profile.append("items", {
			"item_code": item_code,
			"gross_demand": gross_demand,
			"available_inventory": available_inventory,
			"firm_wo_qty": firm_wo_qty,
			"lot_size": _get_lot_size(item_code),
			"due_date": po_item.planned_start_date or frappe.utils.today(),
		})

	profile.save()

	total_net = sum(item.net_demand for item in profile.items)
	return {
		"item_count": len(profile.items),
		"total_net_demand": total_net,
	}


@frappe.whitelist()
def refresh_inventory_and_wo(demand_profile_name: str) -> dict:
	"""Refresh available inventory and firm W/O quantities for all items.

	Re-queries current stock and Work Order status from ERPNext
	without changing gross demand or due dates.

	Args:
		demand_profile_name: Name of the FP Demand Profile.

	Returns:
		Dict with updated item count and total net demand.
	"""
	profile = frappe.get_doc("FP Demand Profile", demand_profile_name)

	for item in profile.items:
		item.available_inventory = _get_available_inventory(item.item_code)
		item.firm_wo_qty = _get_firm_wo_qty(item.item_code)

	profile.save()

	total_net = sum(item.net_demand for item in profile.items)
	return {
		"item_count": len(profile.items),
		"total_net_demand": total_net,
	}


@frappe.whitelist()
def generate_solver_input(demand_profile_name: str) -> list[dict]:
	"""Convert demand profile items into solver-ready job dicts.

	Each item's net demand is split into lots, producing job dicts
	compatible with fp.solver.engine.Job constructor.

	Args:
		demand_profile_name: Name of the FP Demand Profile.

	Returns:
		List of job dicts with keys: job_id, item_code, qty, lot_number,
		due_date, source_demand_id.
	"""
	profile = frappe.get_doc("FP Demand Profile", demand_profile_name)

	jobs = []
	job_counter = 0

	for item in profile.items:
		if not item.net_demand or item.net_demand <= 0:
			continue

		lot_size = item.lot_size or 200
		lot_quantities = split_into_lots(item.net_demand, lot_size)

		for lot_idx, qty in enumerate(lot_quantities):
			job_counter += 1
			jobs.append({
				"job_id": f"JOB-{job_counter:04d}",
				"item_code": item.item_code,
				"qty": qty,
				"lot_number": lot_idx + 1,
				"due_date": str(item.due_date),
				"source_demand_id": f"{demand_profile_name}:{item.idx}",
			})

	return jobs


def _get_available_inventory(item_code: str, warehouse: str | None = None) -> float:
	"""Query actual stock quantity from ERPNext Bin.

	Args:
		item_code: Item to query.
		warehouse: Specific warehouse. If None, sums across all warehouses.

	Returns:
		Available quantity (actual_qty).
	"""
	filters = {"item_code": item_code}
	if warehouse:
		filters["warehouse"] = warehouse

	result = frappe.get_all(
		"Bin",
		filters=filters,
		fields=["sum(actual_qty) as total_qty"],
	)

	return result[0].total_qty or 0 if result else 0


def _get_firm_wo_qty(item_code: str) -> float:
	"""Query total quantity from firm (active) Work Orders.

	Firm WO = Work Orders in Draft, Not Started, or In Process status
	that haven't been fully completed yet.

	Args:
		item_code: Production item to query.

	Returns:
		Sum of (qty - produced_qty) for active Work Orders.
	"""
	result = frappe.get_all(
		"Work Order",
		filters={
			"production_item": item_code,
			"status": ["in", ["Draft", "Not Started", "In Process"]],
			"docstatus": ["<", 2],  # Not cancelled
		},
		fields=["sum(qty - produced_qty) as remaining_qty"],
	)

	return result[0].remaining_qty or 0 if result else 0


def _get_lot_size(item_code: str) -> float:
	"""Get default lot size for an item.

	Checks Item doc for a custom fp_lot_size field, falls back to 200.

	Args:
		item_code: Item to look up.

	Returns:
		Lot size quantity.
	"""
	lot_size = frappe.db.get_value("Item", item_code, "fp_lot_size")
	return lot_size or 200
