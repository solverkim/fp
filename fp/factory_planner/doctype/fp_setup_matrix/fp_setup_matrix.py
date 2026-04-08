import frappe
from frappe import _
from frappe.model.document import Document


class FPSetupMatrix(Document):
	def validate(self) -> None:
		self._validate_different_groups()
		self._validate_transition_time()
		self._validate_unique_combination()

	def _validate_different_groups(self) -> None:
		if self.from_setup_group == self.to_setup_group:
			frappe.throw(_("From Setup Group and To Setup Group must be different."))

	def _validate_transition_time(self) -> None:
		if not self.is_transition_allowed:
			self.setup_time_mins = 0

		if self.setup_time_mins and self.setup_time_mins < 0:
			frappe.throw(_("Setup Time cannot be negative."))

	def _validate_unique_combination(self) -> None:
		"""Ensure (workstation, from_setup_group, to_setup_group) is unique."""
		if not self.workstation or not self.from_setup_group or not self.to_setup_group:
			return

		filters = {
			"workstation": self.workstation,
			"from_setup_group": self.from_setup_group,
			"to_setup_group": self.to_setup_group,
			"name": ["!=", self.name],
		}
		if frappe.db.exists("FP Setup Matrix", filters):
			frappe.throw(
				_("Setup Matrix entry already exists for {0}: {1} → {2}").format(
					self.workstation, self.from_setup_group, self.to_setup_group
				)
			)
