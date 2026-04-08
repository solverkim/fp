import frappe
from frappe import _
from frappe.model.document import Document


class FPTATMaster(Document):
	def validate(self) -> None:
		self._validate_base_tat()
		self._validate_inline_inspection()
		self._validate_wait_time()

	def _validate_base_tat(self) -> None:
		if self.base_tat_mins < 0:
			frappe.throw(_("Base TAT cannot be negative."))

	def _validate_inline_inspection(self) -> None:
		if self.is_inline_inspection and not self.inspection_tat_mins:
			frappe.throw(_("Inspection TAT is required when Inline Inspection is enabled."))

		if not self.is_inline_inspection:
			self.inspection_tat_mins = 0

	def _validate_wait_time(self) -> None:
		if (self.wait_time_mins or 0) < 0:
			frappe.throw(_("Wait Time cannot be negative."))
