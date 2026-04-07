import frappe
from frappe.model.document import Document
from frappe.utils import time_diff_in_seconds


class FPShiftCalendar(Document):
	def validate(self):
		self.calculate_available_capacity()

	def calculate_available_capacity(self):
		if self.is_holiday:
			self.available_capacity_mins = 0
			return

		diff_secs = time_diff_in_seconds(self.end_time, self.start_time)
		total_mins = diff_secs / 60
		self.available_capacity_mins = max(0, total_mins - (self.break_duration_mins or 0))
