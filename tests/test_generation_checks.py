"""Generation-time checks: canaries, distinctive sensitive values, denylist, doc spans."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from eeb import names
from eeb.generator.core import Config
from eeb.generator.instance import (
    GenerationError,
    check_canaries,
    check_sensitive,
    generate,
    resolve_doc_spans,
)
from eeb.rng import Stream


@pytest.fixture()
def inst():  # type: ignore[no-untyped-def]
    i, _ = generate(Config(seed=7))
    return i


def test_checks_pass_on_a_clean_instance(inst) -> None:  # type: ignore[no-untyped-def]
    check_canaries(inst)
    check_sensitive(inst)
    resolve_doc_spans(inst)


def test_every_scoped_row_and_chunk_carries_a_canary(inst) -> None:  # type: ignore[no-untyped-def]
    for table in ("suppliers", "contracts", "purchase_orders", "po_lines", "invoices",
                  "goods_receipts", "payments", "budgets", "supplier_risk_ratings"):
        assert all(names.is_valid_canary(r["row_tag"]) for r in inst.tables[table]), table
    for chunk in inst.tables["doc_chunks"]:
        found = names.CANARY_RE.findall(chunk["text"])
        assert len(found) == 1 and names.is_valid_canary(found[0])


def test_unregistered_canary_shaped_token_fails(inst) -> None:  # type: ignore[no-untyped-def]
    forged = names.canary(Stream(999, "forge"))
    inst.documents[0].rendered += f"\nstray {forged}\n"
    with pytest.raises(GenerationError, match="unregistered"):
        check_canaries(inst)


def test_registered_but_missing_canary_fails(inst) -> None:  # type: ignore[no-untyped-def]
    inst.canaries.append({"canary": names.canary(Stream(998, "x")), "location": {}})
    with pytest.raises(GenerationError, match="never emitted"):
        check_canaries(inst)


def test_sensitive_value_occurring_elsewhere_fails(inst) -> None:  # type: ignore[no-untyped-def]
    leaked = inst.sensitive_values[0]["value"]
    inst.tables["suppliers"][0]["name"] += f" {leaked}"
    with pytest.raises(GenerationError, match="also occurs"):
        check_sensitive(inst)


def test_sensitive_value_with_few_digits_fails(inst) -> None:  # type: ignore[no-untyped-def]
    inst.sensitive_values[0]["value"] = "71.4"
    with pytest.raises(GenerationError, match="significant digits"):
        check_sensitive(inst)


def test_sensitive_values_are_distinctive(inst) -> None:  # type: ignore[no-untyped-def]
    values = [v["value"] for v in inst.sensitive_values]
    assert values and len(values) == len(set(values))
    for v in values:
        assert len(v.replace(".", "").lstrip("0")) >= 6, v
        assert Decimal(v) > 0


def test_doc_phrase_must_be_unique(inst) -> None:  # type: ignore[no-untyped-def]
    f = next(x for x in inst.scenarios if x.get("doc_ref"))
    doc = next(d for d in inst.documents
               if (d.doc_id, d.version) == (f["doc_ref"]["doc_id"], f["doc_ref"]["version"]))
    doc.rendered += "\n" + f["doc_ref"]["phrase"]
    with pytest.raises(GenerationError, match="exactly once"):
        resolve_doc_spans(inst)


def test_doc_spans_point_at_the_phrase(files: dict[str, bytes]) -> None:
    for line in files["gold/scenario_facts.jsonl"].decode().splitlines():
        f = json.loads(line)
        ref = f.get("doc_ref")
        if ref:
            text = files[f"docs/{ref['doc_id']}@v{ref['version']}.md"].decode()
            assert text[ref["start"]:ref["end"]] == ref["phrase"]


def test_denylist_screen() -> None:
    assert names.violates_denylist("Siemens Metals")
    assert names.violates_denylist("Grand TATA Supply")
    assert not names.violates_denylist("Draivel Foundry")


def test_generated_names_pass_denylist(inst) -> None:  # type: ignore[no-untyped-def]
    assert not [s["name"] for s in inst.tables["suppliers"] if names.violates_denylist(s["name"])]


def test_every_document_is_marked_synthetic(files: dict[str, bytes]) -> None:
    docs = [k for k in files if k.startswith("docs/")]
    assert docs and all(b"SYNTHETIC DATA" in files[k] for k in docs)
    assert "SYNTHETIC" in json.loads(files["INSTANCE.json"])["synthetic"]


def test_canary_validity_check_rejects_bad_checksum() -> None:
    c = names.canary(Stream(1, "c"))
    bad = c[:-1] + ("b" if c[-1] != "b" else "c")
    assert names.is_valid_canary(c) and not names.is_valid_canary(bad)
