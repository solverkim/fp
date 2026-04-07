import frappe
from frappe import _
from frappe.model.document import Document


class FPTATMaster(Document):
	def validate(self):
		if self.is_inline_inspection and not self.inspection_tat_mins:
			frappe.throw(_("Inspection TAT is required when Inline Inspection is enabled."))

		if self.base_tat_mins < 0:
			frappe.throw(_("Base TAT cannot be negative."))
