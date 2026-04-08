frappe.ui.form.on("FP Planning Snapshot", {
	refresh(frm) {
		frm.trigger("setup_status_indicator");
		frm.trigger("setup_action_buttons");
	},

	setup_status_indicator(frm) {
		const colors = {
			"Pre Plan": "orange",
			"Draft Plan": "blue",
			"Fixed Plan": "green",
			"Archived": "gray",
		};
		frm.page.set_indicator(
			__(frm.doc.status),
			colors[frm.doc.status] || "gray"
		);
	},

	setup_action_buttons(frm) {
		if (frm.is_new()) return;

		// Pre Plan → Duplicate as Draft
		if (frm.doc.status === "Pre Plan") {
			frm.add_custom_button(
				__("Create Draft Plan"),
				() => frm.trigger("create_draft"),
				__("Actions")
			);
		}

		// Draft Plan → Open in Gantt Tuning
		if (frm.doc.status === "Draft Plan") {
			frm.add_custom_button(
				__("Open in Gantt Tuning"),
				() => {
					frappe.set_route("gantt-tuning");
					// Pre-select this snapshot after navigation
					setTimeout(() => {
						const page = cur_page?.page?.page;
						if (page && page.fields_dict && page.fields_dict.snapshot) {
							page.fields_dict.snapshot.set_value(frm.doc.name);
						}
					}, 500);
				},
				__("View")
			);

			frm.add_custom_button(
				__("Promote to Fixed Plan"),
				() => frm.trigger("promote_to_fixed"),
				__("Actions")
			);
		}

		// Fixed Plan → Open Work Order Tracking
		if (frm.doc.status === "Fixed Plan") {
			frm.add_custom_button(
				__("Work Order Tracking"),
				() => {
					frappe.set_route("wo-tracking");
					setTimeout(() => {
						const page = cur_page?.page?.page;
						if (page && page.fields_dict && page.fields_dict.snapshot) {
							page.fields_dict.snapshot.set_value(frm.doc.name);
						}
					}, 500);
				},
				__("View")
			);
		}

		// Any non-archived → Compare in Dashboard
		if (frm.doc.status !== "Archived") {
			frm.add_custom_button(
				__("Compare in Dashboard"),
				() => frappe.set_route("planning-dashboard"),
				__("View")
			);
		}

		// KPI display in dashboard area
		frm.trigger("show_kpi_dashboard");
	},

	create_draft(frm) {
		frappe.confirm(
			__("Create a Draft Plan from this Pre Plan? The current master data will be inherited."),
			() => {
				frappe.xcall(
					"fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot.duplicate_as_draft",
					{ source_name: frm.doc.name }
				).then((new_name) => {
					frappe.show_alert({
						message: __("Draft Plan created: {0}", [new_name]),
						indicator: "green",
					});
					frappe.set_route("Form", "FP Planning Snapshot", new_name);
				});
			}
		);
	},

	promote_to_fixed(frm) {
		frappe.confirm(
			__(
				"Promote to Fixed Plan? This will:\n" +
				"- Lock this plan for Work Order release\n" +
				"- Archive all other Draft Plans from the same parent\n\n" +
				"This action cannot be undone."
			),
			() => {
				frm.set_value("status", "Fixed Plan");
				frm.save().then(() => {
					frappe.show_alert({
						message: __("Plan is now Fixed. Sibling drafts have been archived."),
						indicator: "green",
					});
				});
			}
		);
	},

	show_kpi_dashboard(frm) {
		if (frm.is_new()) return;

		const kpis = [
			{
				label: __("Tardiness"),
				value: frm.doc.total_tardiness_mins,
				unit: "min",
				threshold: 0,
			},
			{
				label: __("Setup Time"),
				value: frm.doc.total_setup_time_mins,
				unit: "min",
			},
			{
				label: __("Utilization"),
				value: frm.doc.line_utilization_pct,
				unit: "%",
			},
			{
				label: __("Jobs"),
				value: (frm.doc.jobs || []).length,
				unit: "",
			},
		];

		const has_values = kpis.some((k) => flt(k.value) > 0);
		if (!has_values) return;

		const kpi_html = kpis
			.map((k) => {
				const color = k.threshold !== undefined && flt(k.value) > k.threshold
					? "red"
					: "blue";
				return `
					<div class="stat-item">
						<span class="stat-label text-muted">${k.label}</span>
						<span class="stat-value text-${color}">${format_number(flt(k.value))} ${k.unit}</span>
					</div>
				`;
			})
			.join("");

		frm.dashboard.add_section(`<div class="d-flex gap-4">${kpi_html}</div>`);
		frm.dashboard.show();
	},
});
