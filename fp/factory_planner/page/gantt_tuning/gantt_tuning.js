frappe.pages["gantt-tuning"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Gantt Tuning"),
		single_column: true,
	});

	page.set_secondary_action(__("Refresh"), () => gantt_tuning.load_snapshot());

	// Snapshot selector
	page.add_field({
		fieldname: "snapshot",
		label: __("Planning Snapshot"),
		fieldtype: "Link",
		options: "FP Planning Snapshot",
		reqd: 1,
		get_query() {
			return {
				filters: { status: ["in", ["Pre Plan", "Draft Plan"]] },
				order_by: "creation desc",
			};
		},
		change() {
			gantt_tuning.load_snapshot();
		},
	});

	page.add_field({
		fieldname: "workstation_filter",
		label: __("Workstation"),
		fieldtype: "Link",
		options: "Workstation",
		change() {
			gantt_tuning.apply_filter();
		},
	});

	const gantt_tuning = new FPGanttTuning(page);
	wrapper.gantt_tuning = gantt_tuning;
};

frappe.pages["gantt-tuning"].on_page_show = function (wrapper) {
	if (wrapper.gantt_tuning) {
		wrapper.gantt_tuning.on_show();
	}
};

frappe.pages["gantt-tuning"].on_page_hide = function (wrapper) {
	if (wrapper.gantt_tuning) {
		wrapper.gantt_tuning.teardown();
	}
};

class FPGanttTuning {
	constructor(page) {
		this.page = page;
		this.jobs = [];
		this.workstations = [];

		this.$container = $('<div class="fp-gantt-container"></div>').appendTo(
			this.page.main
		);
		this.$toolbar = $('<div class="fp-gantt-toolbar"></div>').appendTo(
			this.$container
		);
		this.$chart = $('<div class="fp-gantt-chart"></div>').appendTo(
			this.$container
		);
		this.$legend = $('<div class="fp-gantt-legend"></div>').appendTo(
			this.$container
		);

		this.setup_toolbar();
	}

	on_show() {
		// restore state if needed
	}

	teardown() {
		// Remove event listeners to prevent duplicates on long-lived pages
		this.$chart.find(".fp-gantt-bar").off("dragstart dragend click");
		this.$chart.find(".fp-gantt-row-bars").off("dragover dragleave drop");
	}

	setup_toolbar() {
		this.$toolbar.html(`
			<div class="btn-group btn-group-sm" role="group">
				<button class="btn btn-default fp-zoom" data-zoom="hour">${__("Hour")}</button>
				<button class="btn btn-default fp-zoom active" data-zoom="day">${__("Day")}</button>
				<button class="btn btn-default fp-zoom" data-zoom="week">${__("Week")}</button>
			</div>
			<div class="fp-gantt-info ml-3">
				<span class="fp-frozen-indicator text-muted">${__("Frozen zone shown in gray")}</span>
			</div>
		`);

		this.$toolbar.on("click", ".fp-zoom", (e) => {
			this.$toolbar.find(".fp-zoom").removeClass("active");
			$(e.currentTarget).addClass("active");
			const zoom = $(e.currentTarget).data("zoom");
			this.set_zoom(zoom);
		});
	}

	load_snapshot() {
		const snapshot = this.page.fields_dict.snapshot.get_value();
		if (!snapshot) return;

		frappe.xcall("frappe.client.get", {
			doctype: "FP Planning Snapshot",
			name: snapshot,
		}).then((doc) => {
			this.snapshot_doc = doc;
			this.jobs = (doc.jobs || []).map((j) => ({ ...j }));
			this.workstations = [...new Set(this.jobs.map((j) => j.workstation))].sort();
			this.render_chart();
			this.render_legend();
		}).catch((e) => {
			frappe.msgprint({
				title: __("Error"),
				message: e.message || __("Failed to load snapshot."),
				indicator: "red",
			});
		});
	}

	apply_filter() {
		const ws = this.page.fields_dict.workstation_filter.get_value();
		this.render_chart(ws);
	}

	render_chart(workstation_filter) {
		const jobs = workstation_filter
			? this.jobs.filter((j) => j.workstation === workstation_filter)
			: this.jobs;

		const rows = this.group_by_workstation(jobs);
		this.$chart.empty();

		if (!jobs.length) {
			this.$chart.html(
				`<div class="text-muted text-center p-5">${__("No jobs found for this snapshot.")}</div>`
			);
			return;
		}

		// Compute time range
		const all_starts = jobs.map((j) => new Date(j.planned_start).getTime());
		const all_ends = jobs.map((j) => new Date(j.planned_end).getTime());
		this.time_start = Math.min(...all_starts);
		this.time_end = Math.max(...all_ends);
		const time_range = this.time_end - this.time_start;

		// Calculate chart width based on zoom level
		const zoom = this.zoom_level || "day";
		const MS_PER_HOUR = 3600000;
		const pixels_per_unit = { hour: 120, day: 60, week: 10 };
		const ms_per_unit = { hour: MS_PER_HOUR, day: MS_PER_HOUR * 24, week: MS_PER_HOUR * 24 * 7 };
		const chart_width = Math.max(
			800,
			(time_range / ms_per_unit[zoom]) * pixels_per_unit[zoom]
		);

		// Build time axis header
		const $time_axis = this.render_time_axis(time_range, zoom, chart_width);

		// Build rows
		const $table = $(`<div class="fp-gantt-table" style="min-width:${chart_width + 160}px"></div>`);
		$table.append($time_axis);

		for (const [ws, ws_jobs] of Object.entries(rows)) {
			const esc_ws = frappe.utils.escape_html(ws);
			const $row = $(`
				<div class="fp-gantt-row">
					<div class="fp-gantt-row-label" title="${esc_ws}">${esc_ws}</div>
					<div class="fp-gantt-row-bars"></div>
				</div>
			`);

			const $bars = $row.find(".fp-gantt-row-bars");

			// Frozen zone overlay
			const frozen_jobs = ws_jobs.filter((j) => j.is_frozen);
			if (frozen_jobs.length) {
				const frozen_end = Math.max(
					...frozen_jobs.map((j) => new Date(j.planned_end).getTime())
				);
				const frozen_width = ((frozen_end - this.time_start) / time_range) * 100;
				$bars.append(
					`<div class="fp-gantt-frozen-zone" style="width:${frozen_width}%"></div>`
				);
			}

			for (const job of ws_jobs) {
				const start = new Date(job.planned_start).getTime();
				const end = new Date(job.planned_end).getTime();
				const left = ((start - this.time_start) / time_range) * 100;
				const width = ((end - start) / time_range) * 100;
				const is_tardy = flt(job.tardiness_mins) > 0;
				const bar_class = is_tardy
					? "fp-gantt-bar fp-gantt-bar--tardy"
					: job.is_frozen
						? "fp-gantt-bar fp-gantt-bar--frozen"
						: "fp-gantt-bar";

				const esc_job_id = frappe.utils.escape_html(job.job_id);
				const esc_item = frappe.utils.escape_html(job.item_code);
				const $bar = $(`
					<div class="${bar_class}"
						 style="left:${left}%;width:${Math.max(width, 0.3)}%"
						 data-job-id="${esc_job_id}"
						 draggable="true"
						 title="${frappe.utils.escape_html(this.build_tooltip(job))}">
						<span class="fp-gantt-bar-label">${esc_item} (${flt(job.qty)})</span>
					</div>
				`);

				$bar.on("click", () => this.show_job_detail(job));
				$bars.append($bar);
			}

			$table.append($row);
		}

		this.$chart.append($table);
		this.setup_drag_drop();
	}

	render_time_axis(time_range, zoom, chart_width) {
		const $axis = $(`
			<div class="fp-gantt-row fp-gantt-time-axis">
				<div class="fp-gantt-row-label" style="font-weight:400;color:var(--text-muted)">${__("Time")}</div>
				<div class="fp-gantt-row-bars fp-gantt-axis-bar" style="position:relative"></div>
			</div>
		`);

		const $bar = $axis.find(".fp-gantt-axis-bar");

		const MS_PER_HOUR = 3600000;
		const step_ms = { hour: MS_PER_HOUR, day: MS_PER_HOUR * 24, week: MS_PER_HOUR * 24 * 7 };
		const step = step_ms[zoom];

		// Align start to the beginning of the unit
		let tick = this.time_start - (this.time_start % step);
		if (tick < this.time_start) tick += step;

		while (tick <= this.time_end) {
			const pct = ((tick - this.time_start) / time_range) * 100;
			const d = new Date(tick);
			let label;
			if (zoom === "hour") {
				label = `${String(d.getHours()).padStart(2, "0")}:00`;
			} else if (zoom === "day") {
				label = `${d.getMonth() + 1}/${d.getDate()}`;
			} else {
				label = `W${this.get_iso_week(d)}`;
			}

			$bar.append(`
				<div class="fp-gantt-tick" style="left:${pct}%">
					<span class="fp-gantt-tick-label">${frappe.utils.escape_html(label)}</span>
				</div>
			`);
			tick += step;
		}

		return $axis;
	}

	get_iso_week(date) {
		const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
		const day_num = d.getUTCDay() || 7;
		d.setUTCDate(d.getUTCDate() + 4 - day_num);
		const year_start = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
		return Math.ceil(((d - year_start) / 86400000 + 1) / 7);
	}

	group_by_workstation(jobs) {
		const groups = {};
		for (const job of jobs) {
			if (!groups[job.workstation]) {
				groups[job.workstation] = [];
			}
			groups[job.workstation].push(job);
		}
		// Sort jobs within each workstation by planned_start
		for (const ws of Object.keys(groups)) {
			groups[ws].sort(
				(a, b) => new Date(a.planned_start) - new Date(b.planned_start)
			);
		}
		return groups;
	}

	build_tooltip(job) {
		const parts = [
			`${__("Job")}: ${job.job_id}`,
			`${__("Item")}: ${job.item_code}`,
			`${__("Qty")}: ${job.qty}`,
			`${__("Start")}: ${job.planned_start}`,
			`${__("End")}: ${job.planned_end}`,
			`${__("Setup")}: ${job.setup_time_mins} min`,
		];
		if (flt(job.tardiness_mins) > 0) {
			parts.push(`${__("TARDY")}: ${job.tardiness_mins} min overdue`);
		}
		if (job.is_frozen) {
			parts.push(__("FROZEN - cannot move"));
		}
		return parts.join("\n");
	}

	show_job_detail(job) {
		const d = new frappe.ui.Dialog({
			title: __("Job Detail: {0}", [frappe.utils.escape_html(job.job_id)]),
			fields: [
				{ fieldtype: "HTML", options: this.job_detail_html(job) },
			],
		});
		if (!job.is_frozen && this.snapshot_doc.status !== "Fixed Plan") {
			d.set_primary_action(__("Edit Timing"), () => {
				d.hide();
				this.open_reschedule_dialog(job);
			});
		}
		d.show();
	}

	job_detail_html(job) {
		const esc = frappe.utils.escape_html;
		const tardy_badge = flt(job.tardiness_mins) > 0
			? `<span class="indicator-pill red">${__("TARDY")} ${flt(job.tardiness_mins)}m</span>`
			: `<span class="indicator-pill green">${__("On Time")}</span>`;
		const frozen_badge = job.is_frozen
			? `<span class="indicator-pill gray">${__("Frozen")}</span>`
			: "";

		const wo_cell = job.work_order
			? `<a href="/app/work-order/${encodeURIComponent(job.work_order)}">${esc(job.work_order)}</a>`
			: __("Not Released");

		return `
			<div class="fp-job-detail">
				<div class="d-flex gap-2 mb-3">${tardy_badge} ${frozen_badge}</div>
				<table class="table table-bordered table-sm">
					<tr><th>${__("Item")}</th><td>${esc(job.item_code)}</td></tr>
					<tr><th>${__("Operation")}</th><td>${esc(job.operation)} (seq ${cint(job.operation_sequence)})</td></tr>
					<tr><th>${__("Workstation")}</th><td>${esc(job.workstation)}</td></tr>
					<tr><th>${__("Qty")}</th><td>${flt(job.qty)}</td></tr>
					<tr><th>${__("Planned Start")}</th><td>${esc(job.planned_start)}</td></tr>
					<tr><th>${__("Planned End")}</th><td>${esc(job.planned_end)}</td></tr>
					<tr><th>${__("Setup Time")}</th><td>${flt(job.setup_time_mins)} min</td></tr>
					<tr><th>${__("Due Date")}</th><td>${esc(job.due_date)}</td></tr>
					<tr><th>${__("Source Demand")}</th><td>${esc(job.source_demand_id || "-")}</td></tr>
					<tr><th>${__("Work Order")}</th><td>${wo_cell}</td></tr>
				</table>
			</div>
		`;
	}

	open_reschedule_dialog(job) {
		const d = new frappe.ui.Dialog({
			title: __("Reschedule Job: {0}", [job.job_id]),
			fields: [
				{
					fieldname: "new_workstation",
					fieldtype: "Link",
					options: "Workstation",
					label: __("Workstation"),
					default: job.workstation,
					reqd: 1,
				},
				{
					fieldname: "new_start",
					fieldtype: "Datetime",
					label: __("New Planned Start"),
					default: job.planned_start,
					reqd: 1,
				},
			],
			primary_action_label: __("Validate & Apply"),
			primary_action(values) {
				d.hide();
				frappe.xcall(
					"fp.factory_planner.page.gantt_tuning.gantt_tuning.validate_reschedule",
					{
						snapshot_name: this.snapshot_doc.name,
						job_id: job.job_id,
						new_workstation: values.new_workstation,
						new_start: values.new_start,
					}
				).then((result) => {
					if (result.valid) {
						frappe.show_alert({
							message: __("Job rescheduled. Setup time recalculated: {0} min", [result.new_setup_time]),
							indicator: "green",
						});
						this.load_snapshot();
					} else {
						frappe.msgprint({
							title: __("Constraint Violation"),
							message: result.violations.join("<br>"),
							indicator: "red",
						});
					}
				}).catch((e) => {
					frappe.msgprint({
						title: __("Error"),
						message: e.message || __("Reschedule request failed."),
						indicator: "red",
					});
				});
			}.bind(this),
		});
		d.show();
	}

	setup_drag_drop() {
		// Drag & drop stubs - will be fully wired when backend API is ready
		this.$chart.find(".fp-gantt-bar:not(.fp-gantt-bar--frozen)").on(
			"dragstart",
			function (e) {
				e.originalEvent.dataTransfer.setData(
					"text/plain",
					$(this).data("job-id")
				);
				$(this).addClass("fp-gantt-bar--dragging");
			}
		);

		this.$chart.find(".fp-gantt-bar").on("dragend", function () {
			$(this).removeClass("fp-gantt-bar--dragging");
		});

		this.$chart.find(".fp-gantt-row-bars").on("dragover", function (e) {
			e.preventDefault();
			$(this).addClass("fp-gantt-row--drop-target");
		});

		this.$chart.find(".fp-gantt-row-bars").on("dragleave", function () {
			$(this).removeClass("fp-gantt-row--drop-target");
		});

		this.$chart.find(".fp-gantt-row-bars").on("drop", (e) => {
			e.preventDefault();
			$(e.currentTarget).removeClass("fp-gantt-row--drop-target");
			const job_id = e.originalEvent.dataTransfer.getData("text/plain");
			const target_ws = $(e.currentTarget)
				.closest(".fp-gantt-row")
				.find(".fp-gantt-row-label")
				.text()
				.trim();

			const job = this.jobs.find((j) => j.job_id === job_id);
			if (!job || !this.snapshot_doc) return;

			if (job.is_frozen) {
				frappe.show_alert({
					message: __("Frozen jobs cannot be moved."),
					indicator: "orange",
				});
				return;
			}

			// Calculate approximate new start from drop position with bounds checking
			const $bars = $(e.currentTarget);
			const bar_rect = $bars[0].getBoundingClientRect();
			const drop_x = e.originalEvent.clientX - bar_rect.left;
			const pct = Math.max(0, Math.min(1, drop_x / bar_rect.width));
			const time_range = this.time_end - this.time_start;
			if (time_range <= 0) return;

			const new_start_ts = this.time_start + pct * time_range;
			const new_start_date = new Date(new_start_ts);

			// Sanity check: timestamp must be a valid date
			if (isNaN(new_start_date.getTime())) {
				frappe.show_alert({
					message: __("Invalid drop position. Please try again."),
					indicator: "orange",
				});
				return;
			}

			const new_start = frappe.datetime.get_datetime_as_string(new_start_date);

			frappe.xcall(
				"fp.factory_planner.page.gantt_tuning.gantt_tuning.validate_reschedule",
				{
					snapshot_name: this.snapshot_doc.name,
					job_id: job_id,
					new_workstation: target_ws,
					new_start: new_start,
				}
			).then((result) => {
				if (result.valid) {
					frappe.show_alert({
						message: __("Job {0} moved to {1}. Setup time: {2} min", [
							job_id,
							target_ws,
							result.new_setup_time,
						]),
						indicator: "green",
					});
					this.load_snapshot();
				} else {
					frappe.msgprint({
						title: __("Constraint Violation"),
						message: result.violations.join("<br>"),
						indicator: "red",
					});
				}
			}).catch((e) => {
				frappe.msgprint({
					title: __("Error"),
					message: e.message || __("Reschedule request failed."),
					indicator: "red",
				});
			});
		});
	}

	render_legend() {
		this.$legend.html(`
			<div class="fp-gantt-legend-items">
				<span class="fp-gantt-legend-item">
					<span class="fp-gantt-legend-color fp-gantt-bar"></span> ${__("Normal")}
				</span>
				<span class="fp-gantt-legend-item">
					<span class="fp-gantt-legend-color fp-gantt-bar--tardy"></span> ${__("Tardy (overdue)")}
				</span>
				<span class="fp-gantt-legend-item">
					<span class="fp-gantt-legend-color fp-gantt-bar--frozen"></span> ${__("Frozen")}
				</span>
				<span class="fp-gantt-legend-item">
					<span class="fp-gantt-legend-color fp-gantt-frozen-zone"></span> ${__("Frozen Zone")}
				</span>
			</div>
		`);
	}

	set_zoom(level) {
		this.zoom_level = level;
		// Zoom adjusts the time range display density
		// Re-render with adjusted scale
		this.render_chart(
			this.page.fields_dict.workstation_filter.get_value() || null
		);
	}
}
