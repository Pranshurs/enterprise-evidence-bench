"""Hand-written expectations taken from the policy *table* in prose.

This is a third, deliberately simple statement of key access rules. It does not read
policy.yaml and does not use either implementation. Both the oracle (unit tests) and the
database (Postgres tests) are checked against it, so a shared misreading of policy.yaml by
the two implementations still shows up as a failure here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Rows = dict[str, list[dict[str, Any]]]
PK = tuple[str, ...]


def keys(rows: list[dict[str, Any]], *cols: str) -> set[PK]:
    return {tuple(str(r[c]) for c in cols) for r in rows}


def suppliers_in(t: Rows, cats: set[str]) -> set[PK]:
    return keys([s for s in t["suppliers"] if s["category_id"] in cats], "supplier_id")


def pos_of_suppliers(t: Rows, sup: set[PK]) -> set[PK]:
    return keys([p for p in t["purchase_orders"] if (p["supplier_id"],) in sup], "po_id")


def invoices_of_pos(t: Rows, pos: set[PK]) -> set[PK]:
    return keys([i for i in t["invoices"] if i["po_id"] is not None and (i["po_id"],) in pos],
                "invoice_id")


def _cm(cats: set[str]) -> Callable[[Rows], dict[str, set[PK]]]:
    def f(t: Rows) -> dict[str, set[PK]]:
        sup = suppliers_in(t, cats)
        pos = pos_of_suppliers(t, sup)
        return {"suppliers": sup, "purchase_orders": pos, "invoices": invoices_of_pos(t, pos),
                "contracts": keys([c for c in t["contracts"] if c["category_id"] in cats],
                                  "contract_id")}
    return f


def _buyer(bu: str) -> Callable[[Rows], dict[str, set[PK]]]:
    def f(t: Rows) -> dict[str, set[PK]]:
        return {"purchase_orders": keys([p for p in t["purchase_orders"] if p["bu_id"] == bu],
                                        "po_id"),
                "suppliers": keys(t["suppliers"], "supplier_id")}
    return f


def _ap(bu: str) -> Callable[[Rows], dict[str, set[PK]]]:
    def f(t: Rows) -> dict[str, set[PK]]:
        inv = keys([i for i in t["invoices"] if i["bu_id"] == bu], "invoice_id")
        return {"invoices": inv,
                "payments": keys([p for p in t["payments"] if (p["invoice_id"],) in inv],
                                 "payment_id")}
    return f


# principal -> function(tables) -> {table: expected visible primary keys}
ROW_EXPECTATIONS: dict[str, Callable[[Rows], dict[str, set[PK]]]] = {
    "cm_met": _cm({"CAT-MET"}),
    "cm_elc": _cm({"CAT-ELC"}),
    "cm_multi": _cm({"CAT-PKG", "CAT-LOG"}),
    "cm_moved": _cm({"CAT-ITH"}),  # the MRO assignment expired on 2026-03-31
    "buyer_in": _buyer("BU-IN"),
    "buyer_eu": _buyer("BU-EU"),
    "ap_in": _ap("BU-IN"),
    "ap_eu": _ap("BU-EU"),
    "ap_uk": _ap("BU-UK"),
    "fin_ctrl": lambda t: {"purchase_orders": keys(t["purchase_orders"], "po_id"),
                           "budgets": keys(t["budgets"], "cost_center_id", "quarter")},
    "risk": lambda t: {"supplier_risk_ratings": keys(t["supplier_risk_ratings"], "supplier_id",
                                                     "assessed_on")},
}

# Principals with no active assignment: no privilege on any data table.
NO_ACCESS = ("buyer_uk_future", "fin_leaver", "nobody")

# Tables no principal may read at all.
NEVER_GRANTED = ("supplier_bank_accounts",)

# Columns no principal may read.
NEVER_COLUMNS = {"supplier_contacts": ("contact_name", "email", "phone")}

# Tables that a role must NOT have any privilege on (from the policy table's prose).
FORBIDDEN_TABLES = {
    "cm_met": ("budgets", "supplier_risk_ratings", "payments"),
    "buyer_in": ("invoices", "contract_price_schedule", "budgets"),
    "ap_eu": ("purchase_orders", "contract_price_schedule", "supplier_risk_ratings"),
    "fin_ctrl": ("supplier_risk_ratings",),
    "legal": ("budgets", "purchase_orders", "supplier_risk_ratings"),
    "risk": ("contract_price_schedule", "purchase_orders"),
}

# Document classifications visible per principal (any scope), from the policy table.
DOC_CLASSES = {
    "fin_ctrl": {"finance_memo", "policy", "faq", "contract", "sla", "exception_memo"},
    "legal": {"contract", "sla", "policy", "faq", "exception_memo", "incident", "legal_opinion"},
    "risk": {"risk_note", "incident", "policy", "faq"},
    "buyer_in": {"policy", "faq"},
}
