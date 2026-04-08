import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import time_diff_in_seconds


class FPShiftCalendar(Document):
	def validate(self) -> None:
		self._validate_times()
		self._validate_break_duration()
		self.calculate_available_capacity()

	def _validate_times(self) -> None:
		if self.is_holiday:
			return

		if not self.start_time or not self.end_time:
			frappe.throw(_("Start Time and End Time are required for non-holiday entries."))

		diff_secs = time_diff_in_seconds(self.end_time, self.start_time)
		if diff_secs <= 0:
			frappe.throw(_("End Time must be after Start Time."))

	def _validate_break_duration(self) -> None:
		if (self.break_duration_mins or 0) < 0:
			frappe.throw(_("Break Duration cannot be negative."))

	def calculate_available_capacity(self) -> None:
		if self.is_holiday:
			self.available_capacity_mins = 0
			return

		diff_secs = time_diff_in_seconds(self.end_time, self.start_time)
		total_mins = diff_secs / 60
		self.available_capacity_mins = max(0, total_mins - (self.break_duration_mins or 0))
