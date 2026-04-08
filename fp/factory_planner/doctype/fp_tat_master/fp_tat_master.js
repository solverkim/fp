frappe.ui.form.on("FP TAT Master", {
	refresh(frm) {
		frm.trigger("toggle_inspection_section");
	},

	is_inline_inspection(frm) {
		frm.trigger("toggle_inspection_section");
	},

	toggle_inspection_section(frm) {
		if (frm.doc.is_inline_inspection) {
			frm.dashboard.set_headline(
				__("Inline inspection enabled: this operation will NOT generate a separate Work Order for QC."),
				"blue"
			);
		} else {
			frm.dashboard.clear_headline();
		}
	},

	item_code(frm) {
		if (!frm.doc.item_code || !frm.doc.operation) return;
		// Pre-fill workstation from default BOM routing if available
		frappe.call({
			method: "frappe.client.get_value",
			args: {
				doctype: "BOM",
				filters: {
					item: frm.doc.item_code,
					is_default: 1,
					is_active: 1,
				},
				fieldname: "name",
			},
			callback(r) {
				if (!r.message || !r.message.name) return;
				frappe.call({
					method: "frappe.client.get_value",
					args: {
						doctype: "BOM Operation",
						filters: {
							parent: r.message.name,
							operation: frm.doc.operation,
						},
						fieldname: "workstation",
					},
					callback(r2) {
						if (r2.message && r2.message.workstation && !frm.doc.workstation) {
							frm.set_value("workstation", r2.message.workstation);
						}
					},
				});
			},
		});
	},
});
