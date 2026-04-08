frappe.pages["wo-tracking"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Work Order Tracking"),
		single_column: true,
	});

	page.set_secondary_action(__("Refresh"), () => tracker.load());

	page.add_field({
		fieldname: "snapshot",
		label: __("Fixed Plan"),
		fieldtype: "Link",
		options: "FP Planning Snapshot",
		reqd: 1,
		get_query() {
			return {
				filters: { status: "Fixed Plan" },
				order_by: "creation desc",
			};
		},
		change() {
			tracker.load();
		},
	});

	page.add_field({
		fieldname: "status_filter",
		label: __("W/O Status"),
		fieldtype: "Select",
		options: "\nNot Released\nNot Started\nIn Process\nCompleted\nStopped",
		change() {
			tracker.apply_filter();
		},
	});

	page.add_field({
		fieldname: "item_filter",
		label: __("Item"),
		fieldtype: "Link",
		options: "Item",
		change() {
			tracker.apply_filter();
		},
	});

	const tracker = new FPWOTracker(page);
	wrapper.tracker = tracker;
};

frappe.pages["wo-tracking"].on_page_show = function (wrapper) {
	if (wrapper.tracker) {
		wrapper.tracker.on_show();
	}
};

frappe.pages["wo-tracking"].on_page_hide = function (wrapper) {
	if (wrapper.tracker) {
		wrapper.tracker.$table.find(".fp-genealogy-btn").off("click");
		wrapper.tracker.$genealogy.empty();
	}
};

class FPWOTracker {
	constructor(page) {
		this.page = page;
		this.jobs = [];

		this.$container = $('<div class="fp-wo-container"></div>').appendTo(
			this.page.main
		);
		this.$summary = $('<div class="fp-wo-summary"></div>').appendTo(
			this.$container
		);
		this.$table = $('<div class="fp-wo-table-wrap"></div>').appendTo(
			this.$container
		);
		this.$genealogy = $('<div class="fp-wo-genealogy"></div>').appendTo(
			this.$container
		);
	}

	on_show() {}

	load() {
		const snapshot = this.page.fields_dict.snapshot.get_value();
		if (!snapshot) return;

		this.$table.html(
			`<div class="text-center p-5 text-muted">${__("Loading...")}</div>`
		);

		frappe.xcall(
			"fp.factory_planner.page.wo_tracking.wo_tracking.get_fixed_plan_jobs",
			{ snapshot_name: snapshot }
		).then((jobs) => {
			this.jobs = jobs;
			this.render_summary(jobs);
			this.render_table(jobs);
			this.$genealogy.empty();
		}).catch((e) => {
			this.$table.html(
				`<div class="text-center p-5 text-danger">${frappe.utils.escape_html(e.message || __("Failed to load jobs."))}</div>`
			);
		});
	}

	apply_filter() {
		const status = this.page.fields_dict.status_filter.get_value();
		const item = this.page.fields_dict.item_filter.get_value();

		let filtered = [...this.jobs];

		if (status) {
			if (status === "Not Released") {
				filtered = filtered.filter((j) => !j.work_order);
			} else {
				filtered = filtered.filter((j) => j.wo_status === status);
			}
		}

		if (item) {
			filtered = filtered.filter((j) => j.item_code === item);
		}

		this.render_summary(filtered);
		this.render_table(filtered);
	}

	render_summary(jobs) {
		const total = jobs.length;
		const released = jobs.filter((j) => j.work_order).length;
		const not_released = total - released;
		const completed = jobs.filter((j) => j.wo_status === "Completed").length;
		const in_process = jobs.filter((j) => j.wo_status === "In Process").length;

		const release_pct = total > 0 ? Math.round((released / total) * 100) : 0;

		this.$summary.html(`
			<div class="fp-wo-summary-cards">
				<div class="fp-wo-stat">
					<span class="fp-wo-stat-num">${total}</span>
					<span class="fp-wo-stat-label">${__("Total Jobs")}</span>
				</div>
				<div class="fp-wo-stat fp-wo-stat--green">
					<span class="fp-wo-stat-num">${released}</span>
					<span class="fp-wo-stat-label">${__("W/O Released")}</span>
				</div>
				<div class="fp-wo-stat fp-wo-stat--orange">
					<span class="fp-wo-stat-num">${not_released}</span>
					<span class="fp-wo-stat-label">${__("Not Released")}</span>
				</div>
				<div class="fp-wo-stat fp-wo-stat--blue">
					<span class="fp-wo-stat-num">${in_process}</span>
					<span class="fp-wo-stat-label">${__("In Process")}</span>
				</div>
				<div class="fp-wo-stat fp-wo-stat--teal">
					<span class="fp-wo-stat-num">${completed}</span>
					<span class="fp-wo-stat-label">${__("Completed")}</span>
				</div>
				<div class="fp-wo-stat">
					<span class="fp-wo-stat-num">${release_pct}%</span>
					<span class="fp-wo-stat-label">${__("Release Rate")}</span>
				</div>
			</div>
		`);
	}

	render_table(jobs) {
		if (!jobs.length) {
			this.$table.html(
				`<div class="text-muted text-center p-5">${__("No jobs found.")}</div>`
			);
			return;
		}

		let rows = "";
		for (const job of jobs) {
			const wo_link = job.work_order
				? `<a href="/app/work-order/${encodeURIComponent(job.work_order)}">${frappe.utils.escape_html(job.work_order)}</a>`
				: `<span class="text-muted">${__("Not Released")}</span>`;

			const status_badge = this.status_badge(job.wo_status, job.work_order);

			const progress_bar = job.work_order
				? `<div class="progress" style="height:6px;min-width:60px">
						<div class="progress-bar" style="width:${job.wo_progress}%"></div>
				   </div>
				   <small class="text-muted">${job.wo_progress}%</small>`
				: "";

			const genealogy_btn = job.source_demand_id
				? `<button class="btn btn-xs btn-default fp-genealogy-btn"
						data-demand="${frappe.utils.escape_html(job.source_demand_id)}"
						title="${__("View Order Genealogy")}">
						<svg class="icon icon-sm"><use href="#icon-hierarchy"></use></svg>
				   </button>`
				: "";

			rows += `
				<tr>
					<td>${frappe.utils.escape_html(job.job_id)}</td>
					<td>${frappe.utils.escape_html(job.item_code)}</td>
					<td class="text-right">${job.qty}</td>
					<td>${frappe.utils.escape_html(job.workstation)}</td>
					<td>${frappe.utils.escape_html(job.planned_start)}</td>
					<td>${frappe.utils.escape_html(job.due_date)}</td>
					<td>${wo_link}</td>
					<td>${status_badge}</td>
					<td>${progress_bar}</td>
					<td>${genealogy_btn}</td>
				</tr>
			`;
		}

		this.$table.html(`
			<table class="table table-bordered table-sm fp-wo-detail-table">
				<thead>
					<tr>
						<th>${__("Job ID")}</th>
						<th>${__("Item")}</th>
						<th class="text-right">${__("Qty")}</th>
						<th>${__("Workstation")}</th>
						<th>${__("Planned Start")}</th>
						<th>${__("Due Date")}</th>
						<th>${__("Work Order")}</th>
						<th>${__("Status")}</th>
						<th>${__("Progress")}</th>
						<th></th>
					</tr>
				</thead>
				<tbody>${rows}</tbody>
			</table>
		`);

		// Bind genealogy buttons
		this.$table.find(".fp-genealogy-btn").on("click", (e) => {
			const demand_id = $(e.currentTarget).data("demand");
			this.show_genealogy(demand_id);
		});
	}

	status_badge(wo_status, work_order) {
		if (!work_order) {
			return `<span class="indicator-pill orange">${__("Not Released")}</span>`;
		}
		const colors = {
			"Not Started": "orange",
			"In Process": "blue",
			"Completed": "green",
			"Stopped": "red",
		};
		const color = colors[wo_status] || "gray";
		return `<span class="indicator-pill ${color}">${__(wo_status || "Unknown")}</span>`;
	}

	show_genealogy(source_demand_id) {
		const snapshot = this.page.fields_dict.snapshot.get_value();

		this.$genealogy.html(
			`<div class="text-center p-3 text-muted">${__("Loading genealogy...")}</div>`
		);

		frappe.xcall(
			"fp.factory_planner.page.wo_tracking.wo_tracking.get_order_genealogy",
			{ snapshot_name: snapshot, source_demand_id }
		).then((data) => {
			this.render_genealogy(data);
		}).catch((e) => {
			this.$genealogy.html(
				`<div class="text-center p-3 text-danger">${frappe.utils.escape_html(e.message || __("Failed to load genealogy."))}</div>`
			);
		});
	}

	render_genealogy(data) {
		let html = `
			<div class="fp-genealogy-panel">
				<div class="fp-genealogy-header">
					<h6>${__("Order Genealogy")}: ${frappe.utils.escape_html(data.source_demand_id)}</h6>
					<span class="text-muted">${__("{0} jobs across {1} lots", [data.total_jobs, Object.keys(data.lots).length])}</span>
					<button class="btn btn-xs btn-default fp-genealogy-close ml-auto">✕</button>
				</div>
				<div class="fp-genealogy-tree">
		`;

		for (const [lot_num, lot_jobs] of Object.entries(data.lots)) {
			const lot_label = cint(lot_num) === 0
				? __("Original (no split)")
				: __("Lot {0}", [lot_num]);

			html += `
				<div class="fp-genealogy-lot">
					<div class="fp-genealogy-lot-header">
						<strong>${lot_label}</strong>
						<span class="text-muted">(${lot_jobs.length} ${__("ops")})</span>
					</div>
					<div class="fp-genealogy-lot-jobs">
			`;

			for (const job of lot_jobs) {
				const wo_text = job.work_order
					? `<a href="/app/work-order/${encodeURIComponent(job.work_order)}">${frappe.utils.escape_html(job.work_order)}</a>`
					: `<span class="text-muted">${__("No W/O")}</span>`;

				html += `
					<div class="fp-genealogy-job">
						<span class="fp-genealogy-seq">${job.operation_sequence}</span>
						<span>${frappe.utils.escape_html(job.operation)}</span>
						<span class="text-muted">@ ${frappe.utils.escape_html(job.workstation)}</span>
						<span class="text-muted">${frappe.utils.escape_html(job.item_code)} × ${job.qty}</span>
						<span>${wo_text}</span>
					</div>
				`;
			}

			html += "</div></div>";
		}

		html += "</div></div>";

		this.$genealogy.html(html);

		this.$genealogy.find(".fp-genealogy-close").on("click", () => {
			this.$genealogy.empty();
		});
	}
}
