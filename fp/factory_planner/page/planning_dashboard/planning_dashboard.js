frappe.pages["planning-dashboard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Planning Dashboard"),
		single_column: true,
	});

	page.set_secondary_action(__("Refresh"), () => dashboard.load());

	page.add_field({
		fieldname: "snapshot_a",
		label: __("Snapshot A (Baseline)"),
		fieldtype: "Link",
		options: "FP Planning Snapshot",
		reqd: 1,
		get_query() {
			return { order_by: "creation desc" };
		},
		change() {
			dashboard.load();
		},
	});

	page.add_field({
		fieldname: "snapshot_b",
		label: __("Snapshot B (Comparison)"),
		fieldtype: "Link",
		options: "FP Planning Snapshot",
		reqd: 1,
		get_query() {
			return { order_by: "creation desc" };
		},
		change() {
			dashboard.load();
		},
	});

	const dashboard = new FPPlanningDashboard(page);
	wrapper.dashboard = dashboard;
};

frappe.pages["planning-dashboard"].on_page_show = function (wrapper) {
	if (wrapper.dashboard) {
		wrapper.dashboard.on_show();
	}
};

frappe.pages["planning-dashboard"].on_page_hide = function (wrapper) {
	// Cleanup to prevent stale state on long-lived pages
	if (wrapper.dashboard) {
		wrapper.dashboard.comparison = null;
	}
};

class FPPlanningDashboard {
	constructor(page) {
		this.page = page;
		this.$container = $('<div class="fp-dashboard-container"></div>').appendTo(
			this.page.main
		);
	}

	on_show() {}

	load() {
		const a = this.page.fields_dict.snapshot_a.get_value();
		const b = this.page.fields_dict.snapshot_b.get_value();
		if (!a || !b) return;

		this.$container.html(
			`<div class="text-center p-5"><span class="loading-text">${__("Loading comparison...")}</span></div>`
		);

		frappe.xcall(
			"fp.factory_planner.doctype.fp_planning_snapshot.fp_planning_snapshot.compare_snapshots",
			{ snapshot_a: a, snapshot_b: b }
		).then((result) => {
			this.comparison = result;
			this.render(result);
		}).catch((e) => {
			this.$container.html(
				`<div class="text-center p-5 text-danger">${frappe.utils.escape_html(e.message || __("Failed to load comparison."))}</div>`
			);
		});
	}

	render(data) {
		this.$container.empty();

		// Header with snapshot info
		this.$container.append(this.render_header(data));

		// KPI cards in split view
		const $split = $('<div class="fp-dashboard-split"></div>');
		$split.append(this.render_kpi_cards(data));
		this.$container.append($split);

		// Detailed KPI comparison table
		this.$container.append(this.render_kpi_table(data));

		// Job count summary
		this.$container.append(this.render_job_summary(data));
	}

	render_header(data) {
		const a = data.snapshot_a;
		const b = data.snapshot_b;
		return `
			<div class="fp-dashboard-header">
				<div class="fp-dashboard-header-item">
					<span class="fp-dashboard-badge fp-badge--baseline">${__("Baseline")}</span>
					<strong>${a.name}</strong>
					<span class="text-muted">(${a.status})</span>
				</div>
				<div class="fp-dashboard-header-vs">${__("vs")}</div>
				<div class="fp-dashboard-header-item">
					<span class="fp-dashboard-badge fp-badge--comparison">${__("Comparison")}</span>
					<strong>${b.name}</strong>
					<span class="text-muted">(${b.status})</span>
				</div>
			</div>
		`;
	}

	render_kpi_cards(data) {
		const kpi_config = [
			{
				key: "total_tardiness_mins",
				label: __("Total Tardiness"),
				unit: "min",
				lower_is_better: true,
				icon: "clock",
			},
			{
				key: "total_setup_time_mins",
				label: __("Total Setup Time"),
				unit: "min",
				lower_is_better: true,
				icon: "tool",
			},
			{
				key: "line_utilization_pct",
				label: __("Line Utilization"),
				unit: "%",
				lower_is_better: false,
				icon: "activity",
			},
			{
				key: "objective_value",
				label: __("Objective (Z)"),
				unit: "",
				lower_is_better: true,
				icon: "target",
			},
			{
				key: "solver_run_time_secs",
				label: __("Solver Runtime"),
				unit: "s",
				lower_is_better: true,
				icon: "zap",
			},
		];

		let html = '<div class="fp-kpi-grid">';
		for (const cfg of kpi_config) {
			const kpi = data.kpis[cfg.key];
			if (!kpi) continue;

			const delta = kpi.delta;
			const pct = kpi.pct_change;
			const improved = cfg.lower_is_better ? delta < 0 : delta > 0;
			const worsened = cfg.lower_is_better ? delta > 0 : delta < 0;
			const indicator = delta === 0
				? "gray"
				: improved
					? "green"
					: "red";
			const arrow = delta === 0 ? "→" : delta > 0 ? "↑" : "↓";

			html += `
				<div class="fp-kpi-card">
					<div class="fp-kpi-card-header">
						<span class="fp-kpi-label">${cfg.label}</span>
					</div>
					<div class="fp-kpi-card-body">
						<div class="fp-kpi-values">
							<div class="fp-kpi-value fp-kpi-value--a">
								<span class="fp-kpi-value-label">${__("A")}</span>
								<span class="fp-kpi-value-num">${format_number(kpi.a_value)}</span>
								<span class="fp-kpi-value-unit">${cfg.unit}</span>
							</div>
							<div class="fp-kpi-value fp-kpi-value--b">
								<span class="fp-kpi-value-label">${__("B")}</span>
								<span class="fp-kpi-value-num">${format_number(kpi.b_value)}</span>
								<span class="fp-kpi-value-unit">${cfg.unit}</span>
							</div>
						</div>
						<div class="fp-kpi-delta indicator-pill ${indicator}">
							${arrow} ${format_number(Math.abs(delta))} ${cfg.unit}
							(${delta >= 0 ? "+" : ""}${format_number(pct)}%)
						</div>
					</div>
				</div>
			`;
		}
		html += "</div>";
		return html;
	}

	render_kpi_table(data) {
		const labels = {
			total_tardiness_mins: __("Total Tardiness (mins)"),
			total_setup_time_mins: __("Total Setup Time (mins)"),
			line_utilization_pct: __("Line Utilization (%)"),
			objective_value: __("Objective Value (Z)"),
			solver_run_time_secs: __("Solver Runtime (s)"),
		};

		let rows = "";
		for (const [key, label] of Object.entries(labels)) {
			const kpi = data.kpis[key];
			if (!kpi) continue;

			const delta_class = kpi.delta === 0
				? ""
				: kpi.delta > 0
					? "text-danger"
					: "text-success";

			rows += `
				<tr>
					<td><strong>${label}</strong></td>
					<td class="text-right">${format_number(kpi.a_value)}</td>
					<td class="text-right">${format_number(kpi.b_value)}</td>
					<td class="text-right ${delta_class}">
						${kpi.delta >= 0 ? "+" : ""}${format_number(kpi.delta)}
					</td>
					<td class="text-right ${delta_class}">
						${kpi.pct_change >= 0 ? "+" : ""}${format_number(kpi.pct_change)}%
					</td>
				</tr>
			`;
		}

		return `
			<div class="fp-dashboard-section">
				<h6 class="fp-dashboard-section-title">${__("KPI Comparison Detail")}</h6>
				<table class="table table-bordered table-sm fp-kpi-table">
					<thead>
						<tr>
							<th>${__("KPI")}</th>
							<th class="text-right">${__("Snapshot A")}</th>
							<th class="text-right">${__("Snapshot B")}</th>
							<th class="text-right">${__("Delta")}</th>
							<th class="text-right">${__("Change %")}</th>
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
		`;
	}

	render_job_summary(data) {
		const jc = data.job_count;
		const diff = jc.b - jc.a;
		const diff_str = diff === 0 ? __("Same") : `${diff > 0 ? "+" : ""}${diff}`;

		return `
			<div class="fp-dashboard-section">
				<h6 class="fp-dashboard-section-title">${__("Job Summary")}</h6>
				<div class="fp-job-summary-row">
					<div class="fp-job-summary-item">
						<span class="text-muted">${__("Snapshot A Jobs")}</span>
						<strong>${jc.a}</strong>
					</div>
					<div class="fp-job-summary-item">
						<span class="text-muted">${__("Snapshot B Jobs")}</span>
						<strong>${jc.b}</strong>
					</div>
					<div class="fp-job-summary-item">
						<span class="text-muted">${__("Difference")}</span>
						<strong>${diff_str}</strong>
					</div>
				</div>
			</div>
		`;
	}
}
