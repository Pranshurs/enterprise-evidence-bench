"""Relational schema of the synthetic enterprise (spec §4.1).

This is the single definition of tables, column types and primary keys. It is consumed by
the generator (to emit rows in a fixed column order), the DDL emitter and the loader, and
the policy validator (to reject references to unknown columns). It holds no
authorization semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA = "eeb"
SEC_SCHEMA = "eeb_sec"


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # text | int | numeric | date | bool
    nullable: bool = False


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    pk: tuple[str, ...]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"{self.name}.{name}")


def _t(name: str, pk: tuple[str, ...], *cols: tuple[str, str] | tuple[str, str, bool]) -> Table:
    columns = tuple(Column(c[0], c[1], bool(c[2]) if len(c) == 3 else False) for c in cols)
    return Table(name, columns, pk)


TABLES: tuple[Table, ...] = (
    _t("business_units", ("bu_id",),
       ("bu_id", "text"), ("name", "text"), ("country", "text"), ("currency", "text")),
    _t("cost_centers", ("cc_id",),
       ("cc_id", "text"), ("bu_id", "text"), ("name", "text")),
    _t("categories", ("category_id",),
       ("category_id", "text"), ("name", "text")),
    _t("items", ("item_id",),
       ("item_id", "text"), ("category_id", "text"), ("name", "text"), ("uom", "text")),
    _t("fx_rates", ("month", "currency"),
       ("month", "date"), ("currency", "text"), ("rate_to_eur", "numeric")),
    _t("suppliers", ("supplier_id",),
       ("supplier_id", "text"), ("name", "text"), ("category_id", "text", True),
       ("country", "text"), ("currency", "text"), ("status", "text"),
       ("onboarded_on", "date"), ("row_tag", "text")),
    _t("supplier_contacts", ("contact_id",),
       ("contact_id", "text"), ("supplier_id", "text"), ("role_title", "text"),
       ("contact_name", "text"), ("email", "text"), ("phone", "text"), ("row_tag", "text")),
    _t("supplier_bank_accounts", ("supplier_id",),
       ("supplier_id", "text"), ("account_ref", "text"), ("bank_ref", "text")),
    _t("contracts", ("contract_id",),
       ("contract_id", "text"), ("supplier_id", "text"), ("category_id", "text"),
       ("bu_id", "text", True), ("effective_from", "date"), ("effective_to", "date", True),
       ("doc_id", "text"), ("payment_terms_days", "int"), ("status", "text"),
       ("row_tag", "text")),
    _t("contract_price_schedule", ("contract_id", "item_id", "effective_from"),
       ("contract_id", "text"), ("item_id", "text"), ("effective_from", "date"),
       ("effective_to", "date", True), ("unit_price", "numeric"), ("currency", "text"),
       ("indexed", "bool"), ("row_tag", "text")),
    _t("purchase_orders", ("po_id",),
       ("po_id", "text"), ("bu_id", "text"), ("cost_center_id", "text", True),
       ("supplier_id", "text"), ("contract_id", "text", True), ("order_date", "date"),
       ("currency", "text"), ("status", "text"), ("row_tag", "text")),
    _t("po_lines", ("po_id", "line_no"),
       ("po_id", "text"), ("line_no", "int"), ("item_id", "text"), ("qty", "int"),
       ("unit_price", "numeric"), ("promised_date", "date"), ("expedited", "bool"),
       ("row_tag", "text")),
    _t("goods_receipts", ("receipt_id",),
       ("receipt_id", "text"), ("po_id", "text"), ("line_no", "int"),
       ("received_date", "date"), ("qty_received", "int"), ("qty_rejected", "int"),
       ("delay_cause", "text", True), ("incident_id", "text", True), ("row_tag", "text")),
    _t("invoices", ("invoice_id",),
       ("invoice_id", "text"), ("po_id", "text", True), ("supplier_id", "text"),
       ("bu_id", "text"), ("invoice_date", "date"), ("currency", "text"),
       ("total", "numeric"), ("row_tag", "text")),
    _t("invoice_lines", ("invoice_id", "line_no"),
       ("invoice_id", "text"), ("line_no", "int"), ("po_line_no", "int", True),
       ("qty", "int"), ("unit_price", "numeric"), ("amount", "numeric"), ("row_tag", "text")),
    _t("payments", ("payment_id",),
       ("payment_id", "text"), ("invoice_id", "text"), ("paid_date", "date"),
       ("amount", "numeric"), ("row_tag", "text")),
    _t("budgets", ("cost_center_id", "quarter"),
       ("cost_center_id", "text"), ("quarter", "text"), ("amount", "numeric"),
       ("currency", "text"), ("row_tag", "text")),
    _t("service_credit_claims", ("claim_id",),
       ("claim_id", "text"), ("contract_id", "text"), ("quarter", "text"),
       ("amount", "numeric"), ("currency", "text"), ("status", "text"), ("row_tag", "text")),
    _t("policy_exceptions", ("exception_id",),
       ("exception_id", "text"), ("po_id", "text"), ("reason", "text"),
       ("approver_role", "text"), ("amount", "numeric"), ("currency", "text"),
       ("approved_on", "date"), ("doc_id", "text"), ("row_tag", "text")),
    _t("supplier_incidents", ("incident_id",),
       ("incident_id", "text"), ("supplier_id", "text"), ("bu_id", "text"),
       ("incident_date", "date"), ("kind", "text"), ("severity", "text"),
       ("force_majeure", "bool"), ("doc_id", "text"), ("row_tag", "text")),
    _t("supplier_risk_ratings", ("supplier_id", "assessed_on"),
       ("supplier_id", "text"), ("assessed_on", "date"), ("rating", "text"),
       ("score", "numeric"), ("notes", "text"), ("row_tag", "text")),
    _t("doc_chunks", ("chunk_id",),
       ("chunk_id", "text"), ("doc_id", "text"), ("version", "int"), ("chunk_index", "int"),
       ("section_kind", "text"), ("classification", "text"),
       ("scope_category", "text", True), ("scope_bu", "text", True),
       ("effective_from", "date"), ("effective_to", "date", True), ("text", "text"),
       ("content_sha256", "text")),
)

BY_NAME: dict[str, Table] = {t.name: t for t in TABLES}


def table(name: str) -> Table:
    return BY_NAME[name]
