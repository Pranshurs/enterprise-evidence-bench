"""Benchmark principals and their role assignments (spec §5.1).

Assignments carry validity windows that are evaluated against the instance's fixed
``today``, never the wall clock. The set deliberately includes the following edge cases:
- a principal with an expired assignment plus a newer one in another category;
- a principal holding two categories at once;
- a future-dated assignment that is not yet active;
- a leaver whose only assignment has expired;
- a principal with no assignments at all.

Each principal holds exactly one role (multi-role principals are rejected by the policy
validator; see ADR-0002).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from eeb.generator.core import Instance

_P: tuple[tuple[str, str, str | None, list[tuple[str | None, str, str | None]]], ...] = (
    # principal_id, display, role, [(param_value, valid_from, valid_to)]
    ("cm_met", "Category manager, raw metals", "category_manager",
     [("CAT-MET", "2023-01-01", None)]),
    ("cm_elc", "Category manager, electronic components", "category_manager",
     [("CAT-ELC", "2023-01-01", None)]),
    ("cm_multi", "Category manager, packaging and logistics", "category_manager",
     [("CAT-PKG", "2023-01-01", None), ("CAT-LOG", "2024-06-01", None)]),
    ("cm_moved", "Category manager moved from MRO to IT hardware", "category_manager",
     [("CAT-MRO", "2023-01-01", "2026-03-31"), ("CAT-ITH", "2026-04-01", None)]),
    ("buyer_in", "Buyer, India", "bu_buyer", [("BU-IN", "2023-01-01", None)]),
    ("buyer_eu", "Buyer, Europe", "bu_buyer", [("BU-EU", "2023-01-01", None)]),
    ("buyer_uk_future", "Buyer, UK (starts after today)", "bu_buyer",
     [("BU-UK", "2026-09-01", None)]),
    ("ap_in", "Accounts payable clerk, India", "ap_clerk", [("BU-IN", "2023-01-01", None)]),
    ("ap_eu", "Accounts payable clerk, Europe", "ap_clerk", [("BU-EU", "2023-01-01", None)]),
    ("ap_uk", "Accounts payable clerk, UK", "ap_clerk", [("BU-UK", "2025-01-01", None)]),
    ("fin_ctrl", "Finance controller", "finance_controller", [(None, "2023-01-01", None)]),
    ("fin_leaver", "Former finance controller (left)", "finance_controller",
     [(None, "2023-01-01", "2026-01-31")]),
    ("legal", "Legal counsel", "legal_counsel", [(None, "2023-01-01", None)]),
    ("risk", "Supplier risk analyst", "risk_analyst", [(None, "2023-01-01", None)]),
    ("nobody", "Employee with no procurement access", None, []),
)

PARAM_NAME = {"category_manager": "category", "bu_buyer": "bu", "ap_clerk": "bu"}


def build(inst: Instance) -> None:
    principals: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    for pid, display, role, grants in _P:
        principals.append({"principal_id": pid, "display_name": display, "role": role,
                           "db_login": f"eebp_{pid}"})
        for i, (value, frm, to) in enumerate(grants, start=1):
            assert role is not None
            assignments.append({
                "assignment_id": f"{pid}#{i}", "principal_id": pid, "role": role,
                "param_name": PARAM_NAME.get(role), "param_value": value,
                "valid_from": dt.date.fromisoformat(frm),
                "valid_to": dt.date.fromisoformat(to) if to else None,
            })
    inst.principals = principals
    inst.assignments = assignments
