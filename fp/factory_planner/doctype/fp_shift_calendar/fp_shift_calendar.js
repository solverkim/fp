frappe.ui.form.on("FP Shift Calendar", {
	refresh(frm) {
		frm.trigger("compute_available_capacity");
	},

	start_time(frm) {
		frm.trigger("compute_available_capacity");
	},

	end_time(frm) {
		frm.trigger("compute_available_capacity");
	},

	break_duration_mins(frm) {
		frm.trigger("compute_available_capacity");
	},

	is_holiday(frm) {
		frm.trigger("compute_available_capacity");
	},

	compute_available_capacity(frm) {
		if (frm.doc.is_holiday) {
			frm.set_value("available_capacity_mins", 0);
			return;
		}

		const start = frm.doc.start_time;
		const end = frm.doc.end_time;
		if (!start || !end) return;

		const start_mins = time_to_minutes(start);
		const end_mins = time_to_minutes(end);
		const break_mins = flt(frm.doc.break_duration_mins) || 0;

		let duration = end_mins - start_mins;
		if (duration < 0) {
			// overnight shift
			duration += 24 * 60;
		}

		const capacity = Math.max(0, duration - break_mins);
		frm.set_value("available_capacity_mins", capacity);
	},
});

function time_to_minutes(time_str) {
	if (!time_str) return 0;
	const parts = time_str.split(":");
	return cint(parts[0]) * 60 + cint(parts[1]);
}
