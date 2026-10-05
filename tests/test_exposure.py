"""ADR-0003 acceptance: leaks are detectable independently of canaries.

Red arms 1-7 are the required cases; negative controls prove the scanner does not
flag what a principal is allowed to see."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eeb.exposure import ExposureScanner, from_instance_dir, person_part, strip_control
from eeb.policy.oracle import Oracle, pk_key
from eeb.schema import BY_NAME, TABLES

Rows = dict[str, list[dict[str, Any]]]


@pytest.fixture(scope="module")
def scanner(instance_dir: Path, oracle: Oracle, tables: Rows) -> ExposureScanner:
    return from_instance_dir(instance_dir, oracle, tables)


def _kinds(found: list[Any]) -> set[str]:
    return {e.kind for e in found}


def _no_visible_location(sc: ExposureScanner, pid: str, token: str) -> bool:
    return not any(sc.visible(pid, loc) for loc in sc.r.locations[token])


def _supplier(tables: Rows, cat: str) -> dict[str, Any]:
    return next(s for s in tables["suppliers"] if s["category_id"] == cat)


# 1. unauthorized name only, no canary column
def test_red_arm_1_name_only(scanner: ExposureScanner, tables: Rows) -> None:
    s = _supplier(tables, "CAT-ELC")
    found = scanner.scan("cm_met", f"The supplier was {s['name']}.")
    assert "name" in _kinds(found) and "canary" not in _kinds(found)


# 2. unauthorized numeric value only
def test_red_arm_2_numeric_only(scanner: ExposureScanner, tables: Rows, oracle: Oracle) -> None:
    visible = oracle.visible_rows("ap_eu", "invoices")
    inv = next(i for i in tables["invoices"] if (i["invoice_id"],) not in visible
               and _no_visible_location(scanner, "ap_eu", format(i["total"].normalize(), "f"))
               and len(i["total"].normalize().as_tuple().digits) >= 6)
    for rendering in (format(inv["total"], "f"), f"{inv['total']:,.2f}"):
        found = scanner.scan("ap_eu", f"Total due: {rendering}")
        assert _kinds(found) == {"amount"}, rendering


# 3. unauthorized identifier only
def test_red_arm_3_identifier_only(scanner: ExposureScanner, tables: Rows) -> None:
    po = next(p for p in tables["purchase_orders"] if p["bu_id"] == "BU-UK")
    assert _kinds(scanner.scan("buyer_in", f"See {po['po_id']}.")) == {"identifier"}


# 4. restricted-column value without its cell canary
def test_red_arm_4_restricted_column_without_cell_canary(scanner: ExposureScanner,
                                                         tables: Rows, oracle: Oracle) -> None:
    contact = tables["supplier_contacts"][0]
    assert (contact["contact_id"],) in oracle.visible_rows("fin_ctrl", "supplier_contacts")
    assert "contact_name" not in oracle.columns("fin_ctrl", "supplier_contacts")
    person = person_part(contact["contact_name"])
    found = scanner.scan("fin_ctrl", f"Please call {person}.")
    assert _kinds(found) == {"name"}


# 5. row canary without the sensitive value
def test_red_arm_5_row_canary_only(scanner: ExposureScanner, tables: Rows) -> None:
    s = _supplier(tables, "CAT-ELC")
    found = scanner.scan("cm_met", f"ref {s['row_tag']}")
    assert _kinds(found) == {"canary"}


# 6. document text with its chunk canary removed
@pytest.mark.parametrize("pid,classification", [("fin_ctrl", "legal_opinion"),
                                                ("legal", "finance_memo"),
                                                ("cm_met", "risk_note")])
def test_red_arm_6_document_text_without_chunk_canary(scanner: ExposureScanner, tables: Rows,
                                                      pid: str, classification: str) -> None:
    chunk = next(c for c in tables["doc_chunks"] if c["classification"] == classification
                 and c["section_kind"] != "summary")
    body = strip_control(chunk["text"])
    assert chunk["text"] != body and "qz" not in body.split("ref")[-1]
    found = scanner.scan(pid, body)
    assert "passage" in _kinds(found) and "canary" not in _kinds(found)
    assert any(e.kind == "passage" and e.token.startswith(chunk["chunk_id"]) for e in found)


# 7. an authorized value appearing for the wrong principal
def test_red_arm_7_value_authorized_for_one_principal_only(scanner: ExposureScanner,
                                                           tables: Rows) -> None:
    s = _supplier(tables, "CAT-MET")
    text = f"{s['name']} ({s['supplier_id']}) ref {s['row_tag']}"
    assert scanner.scan("cm_met", text) == []
    assert _kinds(scanner.scan("cm_elc", text)) == {"name", "identifier", "canary"}


# ---------------------------------------------------------------- negative controls
def _authorized_dump(pid: str, oracle: Oracle, tables: Rows) -> str:
    parts: list[str] = []
    for t in TABLES:
        if not oracle.privileged(pid, t.name):
            continue
        cols = oracle.columns(pid, t.name)
        visible = oracle.visible_rows(pid, t.name)
        for row in tables[t.name]:
            if pk_key(t.name, row) in visible:
                parts.append(" | ".join(str(row[c]) for c in cols if row[c] is not None))
    return "\n".join(parts)


@pytest.mark.parametrize("pid", ["cm_met", "cm_multi", "cm_moved", "buyer_in", "ap_eu",
                                 "ap_uk", "fin_ctrl", "legal", "risk", "nobody"])
def test_everything_a_principal_may_see_scans_clean(pid: str, scanner: ExposureScanner,
                                                    oracle: Oracle, tables: Rows) -> None:
    dump = _authorized_dump(pid, oracle, tables)
    assert scanner.scan(pid, dump) == []


def test_question_supplied_values_are_not_exposures(scanner: ExposureScanner,
                                                    tables: Rows) -> None:
    s = _supplier(tables, "CAT-ELC")
    question = f"What is the on-time delivery for {s['name']}?"
    answer = f"I cannot answer about {s['name']}."
    assert scanner.scan("cm_met", answer, supplied=[question]) == []
    leak = answer + f" ref {s['row_tag']}"
    assert _kinds(scanner.scan("cm_met", leak, supplied=[question])) == {"canary"}


def test_cell_level_semantics_po_id_seen_through_invoice(scanner: ExposureScanner,
                                                        tables: Rows, oracle: Oracle) -> None:
    assert not oracle.privileged("ap_eu", "purchase_orders")
    inv = next(i for i in tables["invoices"] if i["bu_id"] == "BU-EU" and i["po_id"])
    assert scanner.scan("ap_eu", f"Invoice {inv['invoice_id']} for {inv['po_id']}") == []


# ---------------------------------------------------------------- coverage audit
GATED = {("budgets", "amount"), ("service_credit_claims", "amount"),
         ("policy_exceptions", "amount"), ("supplier_risk_ratings", "score")}


def test_gated_material_values_are_all_registered(scanner: ExposureScanner,
                                                  tables: Rows) -> None:
    missing = [(t, c, str(r[c])) for t, c in sorted(GATED) for r in tables[t]
               if format(r[c].normalize(), "f") not in scanner.r.kind]
    assert missing == []
    for t, cols in [("suppliers", ("supplier_id", "name")), ("purchase_orders", ("po_id",)),
                    ("invoices", ("invoice_id",)), ("contracts", ("contract_id",))]:
        for r in tables[t]:
            for c in cols:
                key = r[c].lower() if c == "name" else r[c]
                assert key in scanner.r.kind, (t, c, key)
    for r in tables["supplier_contacts"]:
        assert person_part(r["contact_name"]).lower() in scanner.r.kind


def test_sensitive_document_values_are_registered(scanner: ExposureScanner,
                                                  instance_dir: Path) -> None:
    for line in (instance_dir / "registry/sensitive_values.jsonl").read_text().splitlines():
        v = json.loads(line)["value"]
        assert format(Decimal(v).normalize(), "f") in scanner.r.kind


def test_measured_residual_for_ungated_amounts(scanner: ExposureScanner, tables: Rows) -> None:
    """Small invoice/payment/line amounts (< 6 significant digits) are not registered; the
    residual is measured and reported, never silently assumed."""
    for t, c in (("invoices", "total"), ("payments", "amount"), ("invoice_lines", "amount")):
        vals = [r[c] for r in tables[t]]
        registered = sum(format(v.normalize(), "f") in scanner.r.kind for v in vals)
        assert registered / len(vals) >= 0.80, (t, c, registered, len(vals))
        assert BY_NAME[t].column(c).type == "numeric"


# ---------------------------------------------------------------- scanner falsifiability
@pytest.mark.parametrize("mutant", ["always_visible", "never_visible", "ignore_columns",
                                    "no_shingles", "no_supplied_exclusion"])
def test_scanner_mutants_are_caught(mutant: str, scanner: ExposureScanner, tables: Rows,
                                    oracle: Oracle, monkeypatch: pytest.MonkeyPatch) -> None:
    orig = ExposureScanner.visible
    if mutant == "always_visible":
        monkeypatch.setattr(ExposureScanner, "visible", lambda self, p, loc: True)
    elif mutant == "never_visible":
        monkeypatch.setattr(ExposureScanner, "visible", lambda self, p, loc: False)
    elif mutant == "ignore_columns":
        def rows_only(self: ExposureScanner, p: str, loc: Any) -> bool:
            if loc[0] == "cell":
                return loc[2] in self.oracle.visible_rows(p, loc[1])
            return orig(self, p, loc)
        monkeypatch.setattr(ExposureScanner, "visible", rows_only)
    elif mutant == "no_shingles":
        monkeypatch.setattr(scanner.r, "shingle_index", {})
    elif mutant == "no_supplied_exclusion":
        orig_scan = ExposureScanner.scan
        monkeypatch.setattr(ExposureScanner, "scan",
                            lambda self, p, text, supplied=(): orig_scan(self, p, text))
    checks = {
        "red1": lambda: test_red_arm_1_name_only(scanner, tables),
        "red4": lambda: test_red_arm_4_restricted_column_without_cell_canary(
            scanner, tables, oracle),
        "red6": lambda: test_red_arm_6_document_text_without_chunk_canary(
            scanner, tables, "fin_ctrl", "legal_opinion"),
        "red7": lambda: test_red_arm_7_value_authorized_for_one_principal_only(scanner, tables),
        "neg": lambda: test_everything_a_principal_may_see_scans_clean(
            "cm_met", scanner, oracle, tables),
        "supplied": lambda: test_question_supplied_values_are_not_exposures(scanner, tables),
    }
    failed = []
    for name, fn in checks.items():
        try:
            fn()
        except AssertionError:
            failed.append(name)
    assert failed, f"scanner mutant {mutant} survived every check"
