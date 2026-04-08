frappe.ui.form.on("FP Setup Matrix", {
	refresh(frm) {
		frm.trigger("toggle_transition_warning");
	},

	from_setup_group(frm) {
		frm.trigger("validate_transition_pair");
	},

	to_setup_group(frm) {
		frm.trigger("validate_transition_pair");
	},

	workstation(frm) {
		frm.trigger("validate_transition_pair");
	},

	is_transition_allowed(frm) {
		frm.trigger("toggle_transition_warning");
	},

	validate_transition_pair(frm) {
		const { workstation, from_setup_group, to_setup_group } = frm.doc;
		if (!workstation || !from_setup_group || !to_setup_group) return;

		if (from_setup_group === to_setup_group) {
			frappe.msgprint({
				title: __("Validation Warning"),
				message: __(
					"From and To Setup Groups are the same ({0}). Setup time for same-group transitions is typically zero.",
					[from_setup_group]
				),
				indicator: "orange",
			});
		}

		frappe.call({
			method: "frappe.client.get_count",
			args: {
				doctype: "FP Setup Matrix",
				filters: {
					workstation,
					from_setup_group,
					to_setup_group,
					name: ["!=", frm.doc.name || ""],
				},
			},
			callback(r) {
				if (r.message && r.message > 0) {
					frappe.msgprint({
						title: __("Duplicate Entry"),
						message: __(
							"A Setup Matrix entry already exists for {0}: {1} → {2}. Please check before saving.",
							[workstation, from_setup_group, to_setup_group]
						),
						indicator: "red",
					});
				}
			},
		});
	},

	toggle_transition_warning(frm) {
		if (!frm.doc.is_transition_allowed) {
			frm.dashboard.set_headline(
				__("This transition is BLOCKED. Jobs requiring this changeover cannot be scheduled back-to-back."),
				"red"
			);
		} else {
			frm.dashboard.clear_headline();
		}
	},
});
