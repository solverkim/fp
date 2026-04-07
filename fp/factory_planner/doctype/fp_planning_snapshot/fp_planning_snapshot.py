import frappe
from frappe import _
from frappe.model.document import Document


VALID_TRANSITIONS = {
	"Pre Plan": ["Draft Plan"],
	"Draft Plan": ["Fixed Plan", "Archived"],
	"Fixed Plan": ["Archived"],
	"Archived": [],
}


class FPPlanningSnapshot(Document):
	def validate(self):
		self.validate_status_transition()

	def validate_status_transition(self):
		if self.is_new():
			return

		old_status = self.db_get("status")
		if old_status == self.status:
			return

		allowed = VALID_TRANSITIONS.get(old_status, [])
		if self.status not in allowed:
			frappe.throw(
				_("Cannot transition from {0} to {1}. Allowed: {2}").format(
					old_status, self.status, ", ".join(allowed) or "None"
				)
			)

	def on_update(self):
		if self.status == "Fixed Plan":
			self.archive_sibling_drafts()

	def archive_sibling_drafts(self):
		if not self.parent_snapshot:
			return

		siblings = frappe.get_all(
			"FP Planning Snapshot",
			filters={
				"parent_snapshot": self.parent_snapshot,
				"status": "Draft Plan",
				"name": ["!=", self.name],
			},
			pluck="name",
		)
		for name in siblings:
			frappe.db.set_value("FP Planning Snapshot", name, "status", "Archived")
