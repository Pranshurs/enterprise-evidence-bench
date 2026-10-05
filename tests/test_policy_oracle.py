"""Oracle against hand-written expectations, the shape validator's red arms, and
non-vacuity of the edge cases."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from eeb.policy import schema as pschema
from eeb.policy.oracle import Oracle
from eeb.schema import TABLES
from tests.expectations import (
    DOC_CLASSES,
    FORBIDDEN_TABLES,
    NEVER_COLUMNS,
    NEVER_GRANTED,
    NO_ACCESS,
    ROW_EXPECTATIONS,
)


@pytest.mark.parametrize("pid", sorted(ROW_EXPECTATIONS))
def test_oracle_rows_match_prose_expectations(pid: str, oracle: Oracle,
                                              tables: dict[str, list[dict[str, Any]]]) -> None:
    for table, expected in ROW_EXPECTATIONS[pid](tables).items():
        assert set(oracle.visible_rows(pid, table)) == expected, (pid, table)
        assert expected, f"vacuous expectation {pid}.{table}"


@pytest.mark.parametrize("pid", NO_ACCESS)
def test_inactive_principals_have_no_privilege(pid: str, oracle: Oracle) -> None:
    assert [t.name for t in TABLES if oracle.privileged(pid, t.name)] == []


def test_never_granted_tables_and_columns(oracle: Oracle,
                                          tables: dict[str, list[dict[str, Any]]]) -> None:
    pids = sorted({a["principal_id"] for a in oracle._assignments} | {"nobody"})
    for pid in pids:
        for t in NEVER_GRANTED:
            assert not oracle.privileged(pid, t)
        for t, cols in NEVER_COLUMNS.items():
            assert not set(cols) & set(oracle.columns(pid, t))


@pytest.mark.parametrize("pid", sorted(FORBIDDEN_TABLES))
def test_forbidden_tables(pid: str, oracle: Oracle) -> None:
    for t in FORBIDDEN_TABLES[pid]:
        assert not oracle.privileged(pid, t), (pid, t)


@pytest.mark.parametrize("pid", sorted(DOC_CLASSES))
def test_document_classes(pid: str, oracle: Oracle,
                          tables: dict[str, list[dict[str, Any]]]) -> None:
    visible = oracle.visible_rows(pid, "doc_chunks")
    seen = {c["classification"] for c in tables["doc_chunks"] if (c["chunk_id"],) in visible}
    assert seen == DOC_CLASSES[pid]


def test_ap_clerk_sees_only_payment_terms_of_contracts(
        oracle: Oracle, tables: dict[str, list[dict[str, Any]]]) -> None:
    visible = oracle.visible_rows("ap_eu", "doc_chunks")
    contract_chunks = [c for c in tables["doc_chunks"]
                       if (c["chunk_id"],) in visible and c["classification"] == "contract"]
    assert contract_chunks and {c["section_kind"] for c in contract_chunks} == {"payment_terms"}


def test_category_manager_documents_follow_scope(
        oracle: Oracle, tables: dict[str, list[dict[str, Any]]]) -> None:
    visible = oracle.visible_rows("cm_met", "doc_chunks")
    for c in tables["doc_chunks"]:
        if c["classification"] in ("contract", "sla", "exception_memo", "incident"):
            assert ((c["chunk_id"],) in visible) == (c["scope_category"] == "CAT-MET")


# ---------------------------------------------------------------- edge-case non-vacuity
def test_edge_rows_exist(tables: dict[str, list[dict[str, Any]]]) -> None:
    po_bu = {p["po_id"]: p["bu_id"] for p in tables["purchase_orders"]}
    null_cat = {s["supplier_id"] for s in tables["suppliers"] if s["category_id"] is None}
    assert null_cat, "no null-category supplier"
    assert any(p["supplier_id"] in null_cat for p in tables["purchase_orders"])
    assert any(p["cost_center_id"] is None for p in tables["purchase_orders"])
    assert any(i["po_id"] is None for i in tables["invoices"]), "no non-PO invoice"
    assert any(i["po_id"] and po_bu[i["po_id"]] != i["bu_id"] for i in tables["invoices"])
    assert {p["currency"] for p in tables["purchase_orders"]} == {"INR", "EUR", "GBP"}


def test_null_category_rows_are_denied_to_every_category_manager(
        oracle: Oracle, tables: dict[str, list[dict[str, Any]]]) -> None:
    null_cat = {s["supplier_id"] for s in tables["suppliers"] if s["category_id"] is None}
    null_pos = {(p["po_id"],) for p in tables["purchase_orders"] if p["supplier_id"] in null_cat}
    for pid in ("cm_met", "cm_elc", "cm_multi", "cm_moved"):
        assert not null_pos & oracle.visible_rows(pid, "purchase_orders")
        assert not {(s,) for s in null_cat} & oracle.visible_rows(pid, "suppliers")
    assert null_pos <= oracle.visible_rows("fin_ctrl", "purchase_orders")


def test_cross_unit_invoice_follows_invoice_unit_not_po_unit(
        oracle: Oracle, tables: dict[str, list[dict[str, Any]]]) -> None:
    po_bu = {p["po_id"]: p["bu_id"] for p in tables["purchase_orders"]}
    cross = [i for i in tables["invoices"] if i["po_id"] and po_bu[i["po_id"]] != i["bu_id"]]
    for inv in cross:
        ap_owner = {"BU-IN": "ap_in", "BU-EU": "ap_eu", "BU-UK": "ap_uk"}[inv["bu_id"]]
        ap_po = {"BU-IN": "ap_in", "BU-EU": "ap_eu", "BU-UK": "ap_uk"}[po_bu[inv["po_id"]]]
        assert (inv["invoice_id"],) in oracle.visible_rows(ap_owner, "invoices")
        assert (inv["invoice_id"],) not in oracle.visible_rows(ap_po, "invoices")


def test_non_po_invoices_invisible_to_category_managers(
        oracle: Oracle, tables: dict[str, list[dict[str, Any]]]) -> None:
    non_po = {(i["invoice_id"],) for i in tables["invoices"] if i["po_id"] is None}
    for pid in ("cm_met", "cm_multi"):
        assert not non_po & oracle.visible_rows(pid, "invoices")


# ---------------------------------------------------------------- validator red arms
def _policy() -> dict[str, Any]:
    return copy.deepcopy(pschema.load_policy())


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p["roles"]["category_manager"]["tables"].__setitem__("nope", {}), "unknown table"),
    (lambda p: p["roles"]["category_manager"]["tables"]["suppliers"].__setitem__(
        "columns", ["name"]), "primary key"),
    (lambda p: p["roles"]["bu_buyer"]["tables"]["purchase_orders"].__setitem__(
        "rows", {"eq_param": {"column": "bu_id", "param": "category"}}), "role param"),
    (lambda p: p["roles"]["bu_buyer"]["tables"].pop("purchase_orders"), "not granted"),
    (lambda p: p["roles"]["risk_analyst"]["tables"]["suppliers"].__setitem__(
        "rows", {"in_values": {"column": "status", "values": ["x'; drop"]}}), "safe string"),
    (lambda p: p["roles"]["risk_analyst"]["tables"]["suppliers"].__setitem__(
        "rows", {"bogus": 1}), "unknown operator"),
    (lambda p: p["roles"]["category_manager"]["tables"]["po_lines"].__setitem__(
        "rows", {"in_parent": {"table": "purchase_orders", "fk": ["po_id"], "pk": ["bu_id"]}}),
     "primary key"),
    (lambda p: p.__setitem__("extra", 1), "top level"),
])
def test_validator_rejects(mutate: Any, needle: str) -> None:
    p = _policy()
    mutate(p)
    with pytest.raises(pschema.PolicyError, match=needle):
        pschema.validate(yaml.safe_load(yaml.safe_dump(p)))


def test_validator_rejects_multi_role_principal() -> None:
    import datetime as dt

    p = _policy()
    a = [{"assignment_id": "x#1", "principal_id": "x", "role": "risk_analyst",
          "param_name": None, "param_value": None, "valid_from": dt.date(2024, 1, 1),
          "valid_to": None},
         {"assignment_id": "x#2", "principal_id": "x", "role": "legal_counsel",
          "param_name": None, "param_value": None, "valid_from": dt.date(2024, 1, 1),
          "valid_to": None}]
    with pytest.raises(pschema.PolicyError, match="more than one role"):
        pschema.validate_principals([{"principal_id": "x"}], a, p)
