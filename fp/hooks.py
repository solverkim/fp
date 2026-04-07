app_name = "fp"
app_title = "Factory Planner"
app_publisher = "Q-lynx MESA"
app_description = "Intelligent Production Scheduling for ERPNext"
app_email = "admin@qlynx.com"
app_license = "mit"

required_apps = ["frappe", "erpnext"]

# Fixtures — custom fields on ERPNext doctypes
fixtures = [
	{
		"dt": "Custom Field",
		"filters": [["module", "=", "Factory Planner"]],
	},
]

# Scheduled Tasks
scheduler_events = {
	"daily": [
		"fp.frozen_window.release.release_frozen_window_orders",
		"fp.frozen_window.daily_split.process_daily_split",
	],
}
