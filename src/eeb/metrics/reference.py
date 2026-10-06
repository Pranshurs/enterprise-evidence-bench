"""Independent Python reference for the governed metric layer.

Computes every catalog metric from a principal's visible rows, without SQL. Two uses:
the caller-equivalence tests (layer result == reference) and the out-of-metric-layer
validator, which searches for any metric query whose result reconstructs a case's gold
value.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from eeb.metrics import layer


def quarter(d: Any) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def rnd(x: Decimal, places: int) -> Decimal:
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def view_rows(view: Any, name: str) -> list[dict[str, Any]] | None:
    """Rows of a metric view as ``view`` (a PrincipalView-like object) sees them, or None
    if the caller may not run the view."""
    for t, cols in layer.VIEWS[name]["uses"].items():
        if not view.can_read(t, cols):
            return None
    if name == "budgets":
        return [dict(b) for b in view.rows("budgets")]
    if name == "invoice_lines":
        inv = {i["invoice_id"]: i for i in view.rows("invoices")}
        return [{"amount": il["amount"], "supplier_id": inv[il["invoice_id"]]["supplier_id"],
                 "invoice_bu_id": inv[il["invoice_id"]]["bu_id"],
                 "po_id": inv[il["invoice_id"]]["po_id"],
                 "currency": inv[il["invoice_id"]]["currency"],
                 "invoice_quarter": quarter(inv[il["invoice_id"]]["invoice_date"])}
                for il in view.rows("invoice_lines") if il["invoice_id"] in inv]
    pos = {p["po_id"]: p for p in view.rows("purchase_orders")}
    lines = [ln for ln in view.rows("po_lines") if ln["po_id"] in pos]
    if name == "po_lines":
        return [{**ln, "supplier_id": pos[ln["po_id"]]["supplier_id"],
                 "bu_id": pos[ln["po_id"]]["bu_id"],
                 "cost_center_id": pos[ln["po_id"]]["cost_center_id"],
                 "currency": pos[ln["po_id"]]["currency"],
                 "off_contract": "yes" if pos[ln["po_id"]]["contract_id"] is None else "no",
                 "order_quarter": quarter(pos[ln["po_id"]]["order_date"])} for ln in lines]
    rec = {(r["po_id"], r["line_no"]): r for r in view.rows("goods_receipts")}
    out = []
    for ln in lines:
        r = rec.get((ln["po_id"], ln["line_no"]))
        out.append({"po_id": ln["po_id"], "promised_date": ln["promised_date"],
                    "received_date": r["received_date"] if r else None,
                    "qty_received": r["qty_received"] if r else None,
                    "qty_rejected": r["qty_rejected"] if r else None,
                    "supplier_id": pos[ln["po_id"]]["supplier_id"],
                    "bu_id": pos[ln["po_id"]]["bu_id"],
                    "promised_quarter": quarter(ln["promised_date"])})
    return out


def measure(metric: str, rows: list[dict[str, Any]]) -> Any:
    if metric in ("invoiced_amount", "budget_amount"):
        return rnd(sum((r["amount"] for r in rows), Decimal(0)), 2)
    if metric == "ordered_amount":
        return rnd(sum((r["qty"] * r["unit_price"] for r in rows), Decimal(0)), 2)
    if metric == "effective_unit_cost":
        q = sum(r["qty"] for r in rows)
        return None if q == 0 else rnd(sum((r["qty"] * r["unit_price"] for r in rows),
                                           Decimal(0)) / q, 4)
    if metric == "po_count":
        return len({r["po_id"] for r in rows})
    if metric == "raw_on_time_delivery_pct":
        ok = sum(1 for r in rows if r["received_date"] is not None
                 and r["received_date"] <= r["promised_date"])
        return rnd(Decimal(100 * ok) / len(rows), 2) if rows else None
    if metric == "rejection_rate_pct":
        recv = sum(r["qty_received"] for r in rows if r["qty_received"] is not None)
        rej = sum(r["qty_rejected"] for r in rows if r["qty_rejected"] is not None)
        return None if recv == 0 else rnd(Decimal(100 * rej) / recv, 2)
    raise ValueError(metric)


def run(view: Any, metric: str, group_by: list[str],
        filters: dict[str, list[str]]) -> Any:
    m = layer.catalog()["metrics"][metric]
    rows = view_rows(view, m["view"])
    if rows is None:
        return "denied"
    rows = [r for r in rows if all(str(r[k]) in v for k, v in filters.items())]
    group = list(dict.fromkeys(list(m["always_group_by"]) + group_by))
    if not group:
        return [((), measure(metric, rows) if rows or metric == "po_count" else None)]
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(tuple(str(r[g]) for g in group), []).append(r)
    return sorted((k, measure(metric, v)) for k, v in buckets.items())
