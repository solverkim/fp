frappe.ui.form.on("FP Setup Group", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("View Setup Matrix"), function () {
				frappe.set_route("List", "FP Setup Matrix", {
					from_setup_group: frm.doc.name,
				});
			});
		}
	},
});
