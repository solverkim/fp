import frappe
from frappe import _
from frappe.model.document import Document


class FPSetupMatrix(Document):
	def validate(self):
		if self.from_setup_group == self.to_setup_group:
			frappe.throw(_("From Setup Group and To Setup Group must be different."))

		if not self.is_transition_allowed and self.setup_time_mins > 0:
			self.setup_time_mins = 0
