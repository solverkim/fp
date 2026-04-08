"""FP Solver Config — singleton doctype for solver parameters."""

import frappe
from frappe.model.document import Document


class FPSolverConfig(Document):
    pass


def get_solver_config() -> dict:
    """Read FP Solver Config singleton and return as a plain dict.

    Returns sensible defaults when the doctype has not been saved yet.
    """
    defaults = {
        "alpha": 1000,
        "beta": 1,
        "max_time_secs": 120,
        "num_workers": 4,
        "enable_scip_ensemble": False,
        "scip_max_time_secs": 60,
        "quality_threshold": 0.95,
    }
    try:
        doc = frappe.get_single("FP Solver Config")
        return {
            "alpha": doc.alpha or defaults["alpha"],
            "beta": doc.beta or defaults["beta"],
            "max_time_secs": doc.max_time_secs or defaults["max_time_secs"],
            "num_workers": doc.num_workers or defaults["num_workers"],
            "enable_scip_ensemble": bool(doc.enable_scip_ensemble),
            "scip_max_time_secs": doc.scip_max_time_secs or defaults["scip_max_time_secs"],
            "quality_threshold": doc.quality_threshold or defaults["quality_threshold"],
        }
    except Exception:
        return defaults
