"""Lot sizing strategies for demand splitting."""

from fp.demand.netting import split_into_lots


def fixed_order_quantity(net_demand, lot_size):
	"""Fixed Order Quantity (FOQ) lot sizing."""
	return split_into_lots(net_demand, lot_size)


def lot_for_lot(net_demand):
	"""Lot-for-Lot (LFL) — produce exact net demand as single job."""
	if net_demand <= 0:
		return []
	return [net_demand]
