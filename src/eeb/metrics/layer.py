"""Governed metric layer: views, catalog validation and the compiler (spec §4.4, §12).

Security property (non-negotiable): the layer is never a privileged bypass around RLS.
- Every view is ``security_invoker = true``, so the caller's RLS and column grants apply.
- Every view is owned by ``<ns>_metric_owner``, a NOLOGIN role with no table privileges.
  If invoker security were ever lost, queries would fail closed (permission denied)
  instead of running with an owner's rights.
- There are no SECURITY DEFINER functions (checked by the build gate).

Expressiveness property: the compiler emits one fixed SELECT shape per metric. Callers
choose only declared dimensions and filters, and filter values are bound parameters
restricted to a safe alphabet. The layer cannot express predicates, joins or columns
beyond the catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

from eeb.policy.sqlgen import principal_group, qi, service_role

METRIC_SCHEMA = "eeb_m"
_SAFE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _quarter(expr: str) -> str:
    return f"to_char({expr}, 'YYYY\"Q\"Q')"


# Curated metric views. ``uses`` lists every base (table, column) the view reads, so the
# Python reference can decide whether a caller may run the view at all.
VIEWS: dict[str, dict[str, Any]] = {
    "invoice_lines": {
        "sql": ("SELECT il.invoice_id, il.line_no, il.amount, i.supplier_id, "
                "i.bu_id AS invoice_bu_id, i.po_id, i.currency, "
                f"{_quarter('i.invoice_date')} AS invoice_quarter "
                "FROM eeb.invoice_lines il JOIN eeb.invoices i ON i.invoice_id = il.invoice_id"),
        "uses": {"invoice_lines": ["invoice_id", "line_no", "amount"],
                 "invoices": ["invoice_id", "supplier_id", "bu_id", "po_id", "currency",
                              "invoice_date"]},
    },
    "po_lines": {
        "sql": ("SELECT l.po_id, l.line_no, l.item_id, l.qty, l.unit_price, p.supplier_id, "
                "p.bu_id, p.cost_center_id, p.currency, "
                "CASE WHEN p.contract_id IS NULL THEN 'yes' ELSE 'no' END AS off_contract, "
                f"{_quarter('p.order_date')} AS order_quarter "
                "FROM eeb.po_lines l JOIN eeb.purchase_orders p ON p.po_id = l.po_id"),
        "uses": {"po_lines": ["po_id", "line_no", "item_id", "qty", "unit_price"],
                 "purchase_orders": ["po_id", "supplier_id", "bu_id", "cost_center_id",
                                     "currency", "contract_id", "order_date"]},
    },
    "deliveries": {
        "sql": ("SELECT l.po_id, l.line_no, l.promised_date, r.received_date, "
                "r.qty_received, r.qty_rejected, p.supplier_id, p.bu_id, "
                f"{_quarter('l.promised_date')} AS promised_quarter "
                "FROM eeb.po_lines l JOIN eeb.purchase_orders p ON p.po_id = l.po_id "
                "LEFT JOIN eeb.goods_receipts r ON r.po_id = l.po_id AND r.line_no = l.line_no"),
        "uses": {"po_lines": ["po_id", "line_no", "promised_date"],
                 "purchase_orders": ["po_id", "supplier_id", "bu_id"],
                 "goods_receipts": ["po_id", "line_no", "received_date", "qty_received",
                                    "qty_rejected"]},
    },
    "budgets": {
        "sql": "SELECT cost_center_id, quarter, amount, currency FROM eeb.budgets",
        "uses": {"budgets": ["cost_center_id", "quarter", "amount", "currency"]},
    },
}


def owner_role(ns: str) -> str:
    return f"{ns}_metric_owner"


def view_ddl(ns: str) -> list[str]:
    owner = qi(owner_role(ns))
    out = [f"CREATE ROLE {owner} NOLOGIN NOSUPERUSER NOBYPASSRLS",
           f"CREATE SCHEMA {METRIC_SCHEMA}"]
    for name, v in VIEWS.items():
        out.append(f"CREATE VIEW {METRIC_SCHEMA}.{qi(name)} WITH (security_invoker = true) "
                   f"AS {v['sql']}")
        out.append(f"ALTER VIEW {METRIC_SCHEMA}.{qi(name)} OWNER TO {owner}")
    out.append(f"ALTER SCHEMA {METRIC_SCHEMA} OWNER TO {owner}")
    for grantee in (principal_group(ns), service_role(ns)):
        out.append(f"GRANT USAGE ON SCHEMA {METRIC_SCHEMA} TO {qi(grantee)}")
        out.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA {METRIC_SCHEMA} TO {qi(grantee)}")
    return out


class MetricError(ValueError):
    pass


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    data = yaml.safe_load(resources.files("eeb").joinpath("data/metrics.yaml").read_bytes())
    validate_catalog(data)
    return data  # type: ignore[no-any-return]


def catalog_bytes() -> bytes:
    return resources.files("eeb").joinpath("data/metrics.yaml").read_bytes()


def _view_columns(view: str) -> set[str]:
    sql = VIEWS[view]["sql"]
    select = sql[len("SELECT "):sql.index(" FROM ")]
    cols = set()
    for part in re.split(r",\s*(?![^()]*\))", select):
        part = part.strip()
        m = re.search(r"\bAS\s+(\w+)$", part)
        cols.add(m.group(1) if m else part.split(".")[-1])
    return cols


def validate_catalog(data: Any) -> None:
    if set(data) != {"version", "metrics"}:
        raise MetricError("catalog must have exactly version and metrics")
    for name, m in data["metrics"].items():
        if set(m) != {"definition", "view", "measure", "dimensions", "filters",
                      "always_group_by"}:
            raise MetricError(f"{name}: unexpected keys")
        if m["view"] not in VIEWS:
            raise MetricError(f"{name}: unknown view")
        cols = _view_columns(m["view"])
        for key in ("dimensions", "filters", "always_group_by"):
            unknown = set(m[key]) - cols
            if unknown:
                raise MetricError(f"{name}.{key}: not view columns {sorted(unknown)}")
        if not set(m["always_group_by"]) <= set(m["dimensions"]):
            raise MetricError(f"{name}: always_group_by must be dimensions")


@dataclass(frozen=True)
class CompiledMetric:
    metric: str
    sql: str
    params: tuple[Any, ...]
    group_by: tuple[str, ...]


def compile_metric(name: str, group_by: list[str] | None = None,
                   filters: dict[str, Any] | None = None) -> CompiledMetric:
    cat = catalog()["metrics"]
    if name not in cat:
        raise MetricError(f"unknown metric {name!r}")
    m = cat[name]
    group = list(dict.fromkeys(list(m["always_group_by"]) + list(group_by or [])))
    bad = [g for g in group if g not in m["dimensions"]]
    if bad:
        raise MetricError(f"{name}: undeclared dimensions {bad}")
    where, params = [], []
    for key, value in sorted((filters or {}).items()):
        if key not in m["filters"]:
            raise MetricError(f"{name}: undeclared filter {key!r}")
        values = value if isinstance(value, list) else [value]
        if not values or not all(isinstance(v, str) and _SAFE.match(v) for v in values):
            raise MetricError(f"{name}: filter {key!r} values must match [A-Za-z0-9_-]+")
        where.append(f"{qi(key)} = ANY(%s)")
        params.append(values)
    select = ", ".join([qi(g) for g in group] + [f"{m['measure']} AS value"])
    sql = f"SELECT {select} FROM {METRIC_SCHEMA}.{qi(m['view'])}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        cols = ", ".join(qi(g) for g in group)
        sql += f" GROUP BY {cols} ORDER BY {cols}"
    return CompiledMetric(name, sql, tuple(params), tuple(group))
