"""Governed metric layer: caller-equivalent authorization, the view-owner red arm, and the
experiment's expressiveness boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from eeb.db import agreement
from eeb.db.load import build_database, drop, dsn_for
from eeb.metrics import layer
from eeb.metrics.layer import VIEWS, MetricError, compile_metric
from eeb.policy import sqlgen
from eeb.policy.oracle import Oracle, pk_key
from tests.conftest import unique_ns

Rows = dict[str, list[dict[str, Any]]]
PRINCIPALS = ["cm_met", "cm_moved", "cm_multi", "buyer_in", "ap_in", "ap_eu", "ap_uk",
              "fin_ctrl", "legal", "risk", "nobody"]


# ---------------------------------------------------------------- independent reference
def _q(d: Any) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def _round(x: Decimal, places: int) -> Decimal:
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _visible(oracle: Oracle, pid: str, tables: Rows, t: str) -> list[dict[str, Any]]:
    vis = oracle.visible_rows(pid, t)
    return [r for r in tables[t] if pk_key(t, r) in vis]


def _view_rows(oracle: Oracle, pid: str, tables: Rows, view: str) -> list[dict[str, Any]] | None:
    """Rows of a metric view as the principal would see them, or None if denied."""
    for t, cols in VIEWS[view]["uses"].items():
        if not oracle.privileged(pid, t) or not set(cols) <= set(oracle.columns(pid, t)):
            return None
    if view == "budgets":
        return [{**b} for b in _visible(oracle, pid, tables, "budgets")]
    if view == "invoice_lines":
        inv = {i["invoice_id"]: i for i in _visible(oracle, pid, tables, "invoices")}
        return [{"amount": il["amount"], "supplier_id": inv[il["invoice_id"]]["supplier_id"],
                 "invoice_bu_id": inv[il["invoice_id"]]["bu_id"],
                 "po_id": inv[il["invoice_id"]]["po_id"],
                 "currency": inv[il["invoice_id"]]["currency"],
                 "invoice_quarter": _q(inv[il["invoice_id"]]["invoice_date"])}
                for il in _visible(oracle, pid, tables, "invoice_lines")
                if il["invoice_id"] in inv]
    pos = {p["po_id"]: p for p in _visible(oracle, pid, tables, "purchase_orders")}
    lines = [ln for ln in _visible(oracle, pid, tables, "po_lines") if ln["po_id"] in pos]
    if view == "po_lines":
        return [{**ln, "supplier_id": pos[ln["po_id"]]["supplier_id"],
                 "bu_id": pos[ln["po_id"]]["bu_id"],
                 "cost_center_id": pos[ln["po_id"]]["cost_center_id"],
                 "currency": pos[ln["po_id"]]["currency"],
                 "off_contract": "yes" if pos[ln["po_id"]]["contract_id"] is None else "no",
                 "order_quarter": _q(pos[ln["po_id"]]["order_date"])} for ln in lines]
    rec = {(r["po_id"], r["line_no"]): r for r in _visible(oracle, pid, tables, "goods_receipts")}
    out = []
    for ln in lines:
        r = rec.get((ln["po_id"], ln["line_no"]))
        out.append({"promised_date": ln["promised_date"],
                    "received_date": r["received_date"] if r else None,
                    "qty_received": r["qty_received"] if r else None,
                    "qty_rejected": r["qty_rejected"] if r else None,
                    "supplier_id": pos[ln["po_id"]]["supplier_id"],
                    "bu_id": pos[ln["po_id"]]["bu_id"],
                    "promised_quarter": _q(ln["promised_date"])})
    return out


def _measure(metric: str, rows: list[dict[str, Any]]) -> Any:
    if metric == "invoiced_amount" or metric == "budget_amount":
        return _round(sum((r["amount"] for r in rows), Decimal(0)), 2)
    if metric == "ordered_amount":
        return _round(sum((r["qty"] * r["unit_price"] for r in rows), Decimal(0)), 2)
    if metric == "effective_unit_cost":
        q = sum(r["qty"] for r in rows)
        return None if q == 0 else _round(sum((r["qty"] * r["unit_price"] for r in rows),
                                              Decimal(0)) / q, 4)
    if metric == "po_count":
        return len({r["po_id"] for r in rows})
    if metric == "raw_on_time_delivery_pct":
        ok = sum(1 for r in rows if r["received_date"] is not None
                 and r["received_date"] <= r["promised_date"])
        return _round(Decimal(100 * ok) / len(rows), 2)
    if metric == "rejection_rate_pct":
        recv = sum(r["qty_received"] for r in rows if r["qty_received"] is not None)
        rej = sum(r["qty_rejected"] for r in rows if r["qty_rejected"] is not None)
        return None if recv == 0 else _round(Decimal(100 * rej) / recv, 2)
    raise AssertionError(metric)


def reference(oracle: Oracle, tables: Rows, pid: str, metric: str, group_by: list[str],
              filters: dict[str, list[str]]) -> Any:
    m = layer.catalog()["metrics"][metric]
    rows = _view_rows(oracle, pid, tables, m["view"])
    if rows is None:
        return "denied"
    rows = [r for r in rows if all(str(r[k]) in v for k, v in filters.items())]
    group = list(dict.fromkeys(list(m["always_group_by"]) + group_by))
    if not group:
        return [((), _measure(metric, rows))] if rows else [((), None if metric != "po_count"
                                                             else 0)]
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(tuple(str(r[g]) for g in group), []).append(r)
    return sorted((k, _measure(metric, v)) for k, v in buckets.items())


# ---------------------------------------------------------------- running the layer
@contextmanager
def caller(pg_dsn: str, ns: str, pid: str) -> Iterator[psycopg.Connection[Any]]:
    dsn = dsn_for(pg_dsn, ns, sqlgen.login_role(ns, pid), sqlgen.login_password(ns, pid))
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


def run_layer(pg_dsn: str, ns: str, pid: str, metric: str, group_by: list[str],
              filters: dict[str, list[str]]) -> Any:
    c = compile_metric(metric, group_by, filters)
    with caller(pg_dsn, ns, pid) as conn:
        try:
            rows = conn.execute(c.sql, c.params).fetchall()  # type: ignore[arg-type]
        except errors.InsufficientPrivilege:
            return "denied"
    n = len(c.group_by)
    return sorted((tuple(str(v) for v in r[:n]), r[n]) for r in rows)


def _norm(result: Any) -> Any:
    if result == "denied":
        return result
    return [(k, None if v is None else Decimal(v).normalize()) for k, v in result]


CONFIGS: list[tuple[str, list[str], dict[str, list[str]]]] = [
    ("invoiced_amount", [], {}),
    ("invoiced_amount", ["supplier_id", "invoice_quarter"], {}),
    ("invoiced_amount", ["invoice_bu_id"], {"invoice_quarter": ["2025Q3", "2025Q4"]}),
    ("ordered_amount", ["bu_id", "off_contract"], {}),
    ("ordered_amount", ["supplier_id"], {"order_quarter": ["2025Q3"]}),
    ("effective_unit_cost", ["supplier_id", "order_quarter"], {}),
    ("po_count", ["off_contract"], {}),
    ("raw_on_time_delivery_pct", ["supplier_id", "promised_quarter"], {}),
    ("rejection_rate_pct", ["bu_id"], {}),
    ("budget_amount", ["cost_center_id"], {"quarter": ["2025Q3"]}),
]


@pytest.mark.pg
@pytest.mark.parametrize("pid", PRINCIPALS)
def test_metric_layer_is_caller_equivalent(pid: str, pg_dsn: str, built_db: str,
                                           oracle: Oracle, tables: Rows) -> None:
    """Every configured metric, run as the principal, equals an independent Python
    computation over exactly the rows and columns the oracle grants that principal."""
    for metric, group_by, filters in CONFIGS:
        got = _norm(run_layer(pg_dsn, built_db, pid, metric, group_by, filters))
        want = _norm(reference(oracle, tables, pid, metric, group_by, filters))
        assert got == want, (pid, metric, group_by, filters)


@pytest.mark.pg
def test_reference_covers_both_allowed_and_denied_callers(oracle: Oracle, tables: Rows) -> None:
    outcomes = {pid: reference(oracle, tables, pid, "invoiced_amount", [], {})
                for pid in PRINCIPALS}
    assert {p for p, o in outcomes.items() if o == "denied"} >= {"risk", "nobody", "buyer_in"}
    assert all(o != "denied" for p, o in outcomes.items() if p in ("cm_met", "ap_eu", "fin_ctrl"))


# ---------------------------------------------------------------- view-owner red arm
def _cross_unit_case(tables: Rows) -> tuple[str, str, Decimal]:
    po_bu = {p["po_id"]: p["bu_id"] for p in tables["purchase_orders"]}
    lines: dict[str, list[dict[str, Any]]] = {}
    for il in tables["invoice_lines"]:
        lines.setdefault(il["invoice_id"], []).append(il)
    cross = [i for i in tables["invoices"] if i["po_id"] and po_bu[i["po_id"]] != i["bu_id"]]
    cross.sort(key=lambda i: (len(lines[i["invoice_id"]]), i["invoice_id"]))
    inv = cross[0]
    clerk = {"BU-IN": "ap_in", "BU-EU": "ap_eu", "BU-UK": "ap_uk"}[po_bu[inv["po_id"]]]
    others = [i for i in tables["invoices"] if i["po_id"] == inv["po_id"]
              and i["invoice_id"] != inv["invoice_id"]]
    assert not others, "the PO must have exactly one invoice for a one-row red arm"
    return clerk, inv["po_id"], sum((il["amount"] for il in lines[inv["invoice_id"]]),
                                    Decimal(0))


_REAL_VIEW_DDL = layer.view_ddl


def _privileged_views(ns: str) -> list[str]:
    """The mutant: identical, mathematically correct metric SQL, but definer-rights views
    owned by the (superuser) admin, i.e. executed as the view owner."""
    return [s.replace(" WITH (security_invoker = true)", "") for s in _REAL_VIEW_DDL(ns)
            if not (s.startswith("ALTER VIEW") or s.startswith("ALTER SCHEMA"))]


def _fail_closed_views(ns: str) -> list[str]:
    """Definer-rights views owned by the unprivileged metric owner."""
    return [s.replace(" WITH (security_invoker = true)", "") for s in _REAL_VIEW_DDL(ns)]


@pytest.mark.pg
@pytest.mark.parametrize("variant", ["privileged_owner", "unprivileged_definer"])
def test_red_arm_owner_rights_metric_is_detected(variant: str, pg_dsn: str, instance_dir: Path,
                                                 oracle: Oracle, tables: Rows,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    clerk, po_id, unauthorized_amount = _cross_unit_case(tables)
    query = ("invoiced_amount", [], {"po_id": [po_id]})
    want = reference(oracle, tables, clerk, *query)
    assert want == [], "precondition: the clerk may see no invoice for this PO"
    monkeypatch.setattr(layer, "view_ddl", _privileged_views if variant == "privileged_owner"
                        else _fail_closed_views)
    ns = unique_ns("metricmut")
    build_database(pg_dsn, instance_dir, ns)
    try:
        got = run_layer(pg_dsn, ns, clerk, *query)
        gate = agreement.metric_layer_checks(pg_dsn, ns)
    finally:
        drop(pg_dsn, ns)
    assert _norm(got) != _norm(want), f"{variant}: owner-rights metric was not detected"
    if variant == "privileged_owner":
        # Exactly the one unauthorized invoice leaks through the owner's rights.
        assert len(got) == 1 and Decimal(got[0][1]) == unauthorized_amount
    else:
        assert got == "denied"  # fails closed instead of leaking
    assert any("security_invoker" in p for p in gate)


@pytest.mark.pg
def test_same_query_is_correct_on_the_real_layer(pg_dsn: str, built_db: str, oracle: Oracle,
                                                 tables: Rows) -> None:
    clerk, po_id, _ = _cross_unit_case(tables)
    query = ("invoiced_amount", [], {"po_id": [po_id]})
    assert _norm(run_layer(pg_dsn, built_db, clerk, *query)) == _norm(
        reference(oracle, tables, clerk, *query))


@pytest.mark.pg
def test_build_gate_accepts_the_real_layer(pg_dsn: str, built_db: str) -> None:
    assert agreement.metric_layer_checks(pg_dsn, built_db) == []


# ---------------------------------------------------------------- expressiveness boundary
# Frozen for the §13 experiment: changing the governed catalog is a versioned decision.
METRIC_CATALOG_SHA256 = "e4a993d6d360e9f9a115d83cc9af85fa73369663301f1709ffb3ffd6d3c3a0a3"
FROZEN_METRICS = {"invoiced_amount", "ordered_amount", "effective_unit_cost", "po_count",
                  "raw_on_time_delivery_pct", "rejection_rate_pct", "budget_amount"}


def test_catalog_is_exactly_the_predeclared_metrics() -> None:
    assert set(layer.catalog()["metrics"]) == FROZEN_METRICS
    assert hashlib.sha256(layer.catalog_bytes()).hexdigest() == METRIC_CATALOG_SHA256


@pytest.mark.parametrize("bad", [
    lambda: compile_metric("arbitrary_sql"),
    lambda: compile_metric("invoiced_amount", ["delay_cause"]),
    lambda: compile_metric("invoiced_amount", [], {"amount": ["1"]}),
    lambda: compile_metric("invoiced_amount", [], {"supplier_id": ["x' OR '1'='1"]}),
    lambda: compile_metric("invoiced_amount", [], {"supplier_id": []}),
    lambda: compile_metric("invoiced_amount", [], {"supplier_id": [1]}),
    lambda: compile_metric("raw_on_time_delivery_pct", [], {"delay_cause": ["buyer"]}),
])
def test_compiler_rejects_anything_outside_the_catalog(bad: Any) -> None:
    with pytest.raises(MetricError):
        bad()


def test_compiled_sql_has_one_fixed_shape() -> None:
    c = compile_metric("ordered_amount", ["supplier_id"], {"order_quarter": ["2025Q3"],
                                                          "bu_id": ["BU-IN"]})
    assert c.sql.count(" FROM ") == 1 and "eeb_m.\"po_lines\"" in c.sql
    assert "2025Q3" not in c.sql and "BU-IN" not in c.sql  # values are bound, never inlined
    assert c.params == (["BU-IN"], ["2025Q3"])


def test_metric_views_do_not_expose_out_of_layer_material() -> None:
    used = {(t, c) for v in VIEWS.values() for t, cols in v["uses"].items() for c in cols}
    tables_used = {t for t, _ in used}
    assert not tables_used & {"contract_price_schedule", "fx_rates", "service_credit_claims",
                              "policy_exceptions", "supplier_incidents", "doc_chunks",
                              "contracts", "supplier_risk_ratings", "supplier_bank_accounts",
                              "supplier_contacts", "payments"}
    assert ("goods_receipts", "delay_cause") not in used
    assert ("goods_receipts", "incident_id") not in used
    view_text = " ".join(v["sql"] for v in VIEWS.values())
    assert "contract_id AS" not in view_text and "p.contract_id," not in view_text


def test_out_of_layer_questions_cannot_be_compiled() -> None:
    """The S2 contractual figure needs delay causes; the S3 compliance figure needs
    exceptions and thresholds; S1 price variance needs the price schedule. None of these
    is a column of any metric view, so no dimension or filter can reach them."""
    view_cols = set().union(*(layer._view_columns(v) for v in VIEWS))
    for needed in ("delay_cause", "incident_id", "exception_id", "unit_price_schedule",
                   "rate_to_eur", "payment_terms_days", "contract_id"):
        assert needed not in view_cols
