"""Case building: each validator accepts real cases and rejects a constructed violation.

A validator that never rejects measures nothing, so every one has a red arm here: an input
built to break exactly the rule under test, with the expected rejection named.

The small fixture has too few distinct conflict and temporal questions to fill those
quotas once each family is used only once, so these tests bind a sub-plan. The frozen
corpus is built at default scale.
"""

from __future__ import annotations

import copy
import dataclasses
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eeb.cases import dbcheck
from eeb.cases.build import PROBE_STRATA, CaseBuilder, family_id, probe_slots, probe_stratum
from eeb.cases.templates import TEMPLATES, Template
from eeb.cases.validate import (
    metric_layer_membership,
    missing_evidence,
    necessity,
    restricted_probe,
)
from eeb.cases.view import InstanceData
from eeb.db.load import build_database, drop, read_jsonl
from tests.conftest import SEED, unique_ns

C_SLOTS_ON_SMALL = 8
SLOTS_ON_SMALL = {"C": C_SLOTS_ON_SMALL, "T": 25}
# The small fixture holds few partially-visible answers; the corpus gate checks the real share.
PROBE_SHARE_ON_SMALL = (1, 60)
BY_ID: dict[str, Template] = {t.id: t for t in TEMPLATES}


@pytest.fixture(scope="module")
def data(instance_dir: Path) -> InstanceData:
    return InstanceData.load(instance_dir)


@pytest.fixture(scope="module")
def plan(instance_dir: Path) -> list[dict[str, Any]]:
    full = read_jsonl(instance_dir / "cases/plan.jsonl")
    keep: set[str] = set()
    for cls, n in SLOTS_ON_SMALL.items():
        keep |= set(sorted(s["case_id"] for s in full if s["class"] == cls)[:n])
    return [s for s in full if s["class"] not in SLOTS_ON_SMALL or s["case_id"] in keep]


@pytest.fixture(scope="module")
def cases(data: InstanceData, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL).build(plan)


def _first_valid(data: InstanceData, template_id: str, cls: str, want_ool: bool | None
                 ) -> tuple[CaseBuilder, Template, dict[str, Any], str, dict[str, Any]]:
    b = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
    t = BY_ID[template_id]
    for slots, pid, _ in b.candidates(t, False):
        v = b._validated(t, slots, pid, cls, want_ool)
        if v is not None:
            return b, t, slots, pid, v
    raise AssertionError(f"no valid binding for {template_id}")


# ------------------------------------------------------------------ the build as a whole
def test_sub_plan_is_filled_slot_for_slot(cases: list[dict[str, Any]],
                                          plan: list[dict[str, Any]]) -> None:
    assert [c["case_id"] for c in cases] == [s["case_id"] for s in plan]
    for c, s in zip(cases, plan, strict=True):
        assert (c["class"], c["split"], c["group_id"], c["overlays"]) == (
            s["class"], s["split"], s["group_id"], s["overlays"])


def test_build_is_deterministic(data: InstanceData, plan: list[dict[str, Any]],
                                cases: list[dict[str, Any]]) -> None:
    assert CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL).build(plan) == cases


# ------------------------------------------------------------------ family dedup
def test_a_family_is_used_once_outside_counterfactual_groups(
        cases: list[dict[str, Any]]) -> None:
    sizes = Counter(c["family_id"] for c in cases)
    groups = {c["family_id"] for c in cases if c["group_id"]}
    assert all(sizes[f] == 2 for f in groups)
    assert all(n == 1 for f, n in sizes.items() if f not in groups)
    for f in groups:
        a, b = (c for c in cases if c["family_id"] == f)
        assert a["group_id"] == b["group_id"] and a["question"] == b["question"]
        assert a["principal_id"] != b["principal_id"]


def test_red_arm_without_the_family_rule_a_question_is_reused(
        data: InstanceData, plan: list[dict[str, Any]]) -> None:
    slots_c = [s for s in plan if s["class"] == "C"]

    class Forgetful(set[str]):
        def add(self, element: str) -> None:  # the rule's only memory
            return None

    def families(forget: bool) -> list[str]:
        b = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
        if forget:
            b.families = Forgetful()
        return [b.fill_single(s, i)["family_id"] for i, s in enumerate(slots_c)]

    kept, forgotten = families(False), families(True)
    assert len(set(kept)) == len(kept) == C_SLOTS_ON_SMALL
    assert len(set(forgotten)) < len(forgotten)  # the same question, asked by other principals


# ------------------------------------------------------------------ authorization
def test_answering_principals_can_read_all_their_evidence(data: InstanceData) -> None:
    checked = 0
    for tid in ("S.invoiced_amount", "X.raw_otd_gap", "D.otd_target", "C.payment_terms"):
        _, _, _, pid, v = _first_valid(data, tid, tid[0], None)
        assert missing_evidence(v["gold"], data.view(pid)) == []
        checked += len(v["gold"]["facts"])
    assert checked >= 6


def test_red_arm_unreadable_evidence_is_reported(data: InstanceData) -> None:
    _, _, _, pid, v = _first_valid(data, "X.raw_otd_gap", "X", None)
    gold = v["gold"]
    # A principal with no role reads nothing.
    assert len(missing_evidence(gold, data.view("nobody"))) == len(
        [f for f in gold["facts"] if f["source"] == "doc"]) + sum(
        len(f["uses"]) for f in gold["facts"] if f["source"] == "sql")
    # One column outside the asker's grant.
    col = copy.deepcopy(gold)
    sql = next(f for f in col["facts"] if f["source"] == "sql")
    sql["uses"] = {"supplier_bank_accounts": ["iban"]}
    assert missing_evidence(col, data.view(pid)) == [
        f"{sql['fact_id']}: cannot read supplier_bank_accounts(iban)"]
    # One chunk the asker cannot see.
    doc = copy.deepcopy(gold)
    d = next(f for f in doc["facts"] if f["source"] == "doc")
    d["doc_ref"]["chunk_id"] = "no-such-chunk"
    assert missing_evidence(doc, data.view(pid)) == [
        f"{d['fact_id']}: chunk no-such-chunk not visible"]


def test_red_arm_builder_rejects_a_principal_who_lacks_evidence(data: InstanceData) -> None:
    b, t, slots, _, _ = _first_valid(data, "D.otd_target", "D", None)
    before = b.rejections[f"{t.id}: principal lacks evidence"]
    assert b._validated(t, slots, "nobody", "D", None) is None
    assert b.rejections[f"{t.id}: principal lacks evidence"] == before + 1


def test_denied_group_member_is_denied_a_necessary_unit(data: InstanceData,
                                                        cases: list[dict[str, Any]]) -> None:
    denied = [c for c in cases if c["group_member"] == "denied"]
    assert len(denied) >= 60
    by_id = {c["case_id"]: c for c in cases}
    for a in denied:
        x = by_id[a["counterfactual_of"]]
        assert a["expected_outcome"] == "ABSTAIN"
        assert a["abstention_condition"] == "not_authorized"
        assert a["denied_evidence"] and a["gold_facts"] == []
        assert x["expected_outcome"] == "ANSWER" and x["restricted_probe"] is None
        # Re-derive the denial from the instance, not from the recorded reason.
        t = BY_ID[x["template_id"]]
        b = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
        full = b._global_gold(t, x["slots"])
        assert full is not None
        unreadable = missing_evidence(full, data.view(a["principal_id"]))
        if not unreadable:
            mine = b._gold(t, x["slots"], a["principal_id"])
            want = {f["fact_id"]: f["value"] for f in full["facts"]}
            assert mine is None or any(f["source"] == "sql" and want[f["fact_id"]] != f["value"]
                                       for f in mine["facts"])


# ------------------------------------------------------------------ cross-source necessity
def test_cross_source_cases_need_both_sources(data: InstanceData) -> None:
    for tid in ("X.raw_otd_gap", "X.raw_otd_met_target", "X.rejection_within_threshold",
                "X.service_credit_entitlement"):
        _, _, _, _, v = _first_valid(data, tid, "X", None)
        n = v["necessity"]
        assert not n["sql_alone_sufficient"] and not n["docs_alone_sufficient"]
        assert n["sql_alone_lacks"] and n["docs_alone_lack"]


def test_red_arm_single_source_case_is_not_cross_source(data: InstanceData) -> None:
    for tid, verdict in (("S.invoiced_amount", "sql_alone_sufficient"),
                         ("D.otd_target", "docs_alone_sufficient")):
        b, t, slots, pid, v = _first_valid(data, tid, tid[0], None)
        assert v["necessity"][verdict] is True
        before = b.rejections[f"{t.id}: cross-source necessity not proven"]
        assert b._validated(t, slots, pid, "X", None) is None
        assert b.rejections[f"{t.id}: cross-source necessity not proven"] == before + 1


def test_red_arm_document_value_also_held_in_the_database(data: InstanceData) -> None:
    """If the 'document' fact can be read off the contract's own row, SQL alone suffices."""
    _, _, slots, _, v = _first_valid(data, "X.raw_otd_gap", "X", None)
    gold = copy.deepcopy(v["gold"])
    d = next(f for f in gold["facts"] if f["source"] == "doc")
    cid = d["doc_ref"]["doc_id"].split("DOC-MSA-")[-1].split("DOC-SLA-")[-1]
    row = next(c for c in data.tables["contracts"] if c["contract_id"] in d["doc_ref"]["doc_id"])
    assert cid and necessity(data, slots, gold)["sql_alone_sufficient"] is False
    d["value"] = Decimal(row["payment_terms_days"])
    assert necessity(data, slots, gold)["sql_alone_sufficient"] is True


def test_red_arm_database_value_also_printed_in_a_document(data: InstanceData) -> None:
    """If the 'SQL' fact is printed in a document the case cites, documents suffice. The
    cited SLA names the contract but not the supplier, so a search keyed only on the
    question's slots would miss it."""
    _, _, slots, _, v = _first_valid(data, "X.raw_otd_gap", "X", None)
    gold = copy.deepcopy(v["gold"])
    assert necessity(data, slots, gold)["docs_alone_sufficient"] is False
    doc_value = next(f for f in gold["facts"] if f["source"] == "doc")["value"]
    next(f for f in gold["facts"] if f["source"] == "sql")["value"] = doc_value
    assert necessity(data, slots, gold)["docs_alone_sufficient"] is True


# ------------------------------------------------------------------ metric layer
def test_in_layer_and_out_of_layer_cases_are_told_apart(data: InstanceData) -> None:
    for tid, cls, ool in (("S.invoiced_amount", "S", False), ("S.raw_otd", "S", False),
                          ("S.late_line_value", "S", True), ("X.raw_otd_gap", "X", False),
                          ("X.service_credit_entitlement", "X", True)):
        _, _, _, _, v = _first_valid(data, tid, cls, ool)
        assert v["metric_layer"]["in_metric_layer"] is (not ool), tid
        assert bool(v["metric_layer"]["unreconstructible"]) is ool, tid


def test_red_arm_wrong_layer_designation_is_rejected(data: InstanceData) -> None:
    b, t, slots, pid, _ = _first_valid(data, "S.invoiced_amount", "S", False)
    reason = f"{t.id}: designated out-of-layer but reconstructible"
    before = b.rejections[reason]
    assert b._validated(t, slots, pid, "S", True) is None
    assert b.rejections[reason] == before + 1

    b, t, slots, pid, _ = _first_valid(data, "S.late_line_value", "S", True)
    reason = f"{t.id}: designated in-layer but not reconstructible"
    before = b.rejections[reason]
    assert b._validated(t, slots, pid, "S", False) is None
    assert b.rejections[reason] == before + 1


def test_red_arm_out_of_layer_answer_equal_to_a_governed_metric(data: InstanceData) -> None:
    """An out-of-layer fact whose value a permitted metric query returns is reconstructible."""
    b, _, slots, pid, v = _first_valid(data, "S.late_line_value", "S", True)
    _, _, in_slots, in_pid, in_v = _first_valid(data, "S.invoiced_amount", "S", False)
    gold = copy.deepcopy(v["gold"])
    assert metric_layer_membership(b.search, data.view(pid), slots, gold) == {
        "in_metric_layer": False, "unreconstructible": ["late_line_value"]}
    gold["facts"][0]["value"] = in_v["gold"]["facts"][0]["value"]
    assert metric_layer_membership(b.search, data.view(in_pid), in_slots, gold) == {
        "in_metric_layer": True, "unreconstructible": []}


# ------------------------------------------------------------------ restricted-value probe
def test_restricted_probe_records_only_values_that_differ() -> None:
    mine = {"facts": [{"fact_id": "a", "source": "sql", "value": Decimal("10.00")},
                      {"fact_id": "b", "source": "sql", "value": 3},
                      {"fact_id": "c", "source": "doc", "value": 1}]}
    everyone = {"facts": [{"fact_id": "a", "source": "sql", "value": Decimal("25.50")},
                          {"fact_id": "b", "source": "sql", "value": 3},
                          {"fact_id": "c", "source": "doc", "value": 2}]}
    assert restricted_probe(mine, everyone) == [
        {"fact_id": "a", "authorized_value": Decimal("10.00"),
         "unauthorized_value": Decimal("25.50")}]
    assert restricted_probe(mine, mine) == []


def test_restricted_probe_on_a_principal_who_sees_part_of_the_rows(data: InstanceData) -> None:
    """Some principal sees only part of a supplier's invoices; the probe must capture it."""
    b = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
    t = BY_ID["S.invoiced_amount"]
    found = 0
    for slots in b.bindings(t):
        full = b._global_gold(t, slots)
        if full is None:
            continue
        for p in data.principals:
            mine = b._gold(t, slots, p["principal_id"])
            if mine is None:
                continue
            probe = restricted_probe(mine, full)
            assert bool(probe) == (mine["facts"][0]["value"] != full["facts"][0]["value"])
            found += bool(probe)
    assert found > 0


# ------------------------------------------------------------------ gold SQL in Postgres
@pytest.fixture(scope="module")
def case_db(pg_dsn: str, instance_dir: Path) -> Any:
    ns = unique_ns("cases")
    build_database(pg_dsn, instance_dir, ns)
    yield ns
    drop(pg_dsn, ns)


def _sql_case(cases: list[dict[str, Any]], template_id: str) -> dict[str, Any]:
    return copy.deepcopy(next(c for c in cases if c["template_id"] == template_id
                              and c["expected_outcome"] == "ANSWER"))


def test_every_sql_gold_agrees_under_the_askers_login(pg_dsn: str, case_db: str,
                                                      cases: list[dict[str, Any]]) -> None:
    r = dbcheck.check_case_gold_sql(pg_dsn, case_db, cases)
    assert r["problems"] == []
    assert r["sql_facts_checked"] == sum(1 for c in cases for f in c["gold_facts"]
                                         if f["source"] == "sql") > 200
    assert len(r["principals"]) >= 6


def test_red_arm_wrong_gold_value(pg_dsn: str, case_db: str,
                                  cases: list[dict[str, Any]]) -> None:
    c = _sql_case(cases, "S.invoiced_amount")
    f = c["gold_facts"][0]
    f["value"] = format(Decimal(f["value"]) + Decimal("0.02"), "f")
    r = dbcheck.check_case_gold_sql(pg_dsn, case_db, [c])
    assert [p["problem"] for p in r["problems"]] == ["principal_value_mismatch"]


def test_red_arm_value_only_another_principal_would_get(pg_dsn: str, case_db: str,
                                                        cases: list[dict[str, Any]]) -> None:
    """Gold computed for one principal is wrong for a principal who sees other rows."""
    c = _sql_case(cases, "S.invoiced_amount")
    c["principal_id"] = "nobody"
    r = dbcheck.check_case_gold_sql(pg_dsn, case_db, [c])
    assert [(p["problem"], p.get("sqlstate")) for p in r["problems"]] == [
        ("execution_error", "42501")]


def test_red_arm_gold_sql_that_does_not_run(pg_dsn: str, case_db: str,
                                            cases: list[dict[str, Any]]) -> None:
    c = _sql_case(cases, "S.raw_otd")
    c["gold_facts"][0]["gold_sql"] = "SELECT no_such_column FROM eeb.po_lines"
    r = dbcheck.check_case_gold_sql(pg_dsn, case_db, [c])
    assert [(p["problem"], p["sqlstate"]) for p in r["problems"]] == [
        ("execution_error", "42703")]


def test_red_arm_wrong_unrestricted_probe_value(pg_dsn: str, case_db: str,
                                                cases: list[dict[str, Any]]) -> None:
    c = next(copy.deepcopy(x) for x in cases if x["template_id"] == "S.invoiced_amount"
             and x["principal_id"] == "fin_ctrl")
    f = c["gold_facts"][0]
    # fin_ctrl reads every invoice, so the unrestricted value is the gold value itself.
    c["restricted_probe"] = [{"fact_id": f["fact_id"], "authorized_value": f["value"],
                              "unauthorized_value": f["value"]}]
    r = dbcheck.check_case_gold_sql(pg_dsn, case_db, [c])
    assert r["problems"] == [] and r["restricted_probes_checked"] == 1
    c["restricted_probe"][0]["unauthorized_value"] = format(
        Decimal(f["value"]) + Decimal("1.00"), "f")
    r = dbcheck.check_case_gold_sql(pg_dsn, case_db, [c])
    assert [p["problem"] for p in r["problems"]] == ["unrestricted_value_mismatch"]


def test_family_id_ignores_the_asking_principal() -> None:
    t = BY_ID["S.invoiced_amount"]
    a = family_id(t, {"supplier_id": "SUP-0001", "quarter": "2025Q1"})
    assert a == family_id(t, {"quarter": "2025Q1", "supplier_id": "SUP-0001"})
    assert a != family_id(t, {"supplier_id": "SUP-0001", "quarter": "2025Q2"})
    assert a != family_id(BY_ID["S.raw_otd"], {"supplier_id": "SUP-0001", "quarter": "2025Q1"})


# ------------------------------------------------------------------ rules with one caller
def test_red_arm_cited_document_is_searched_when_no_slot_names_it(data: InstanceData) -> None:
    """A question keyed on a currency names no supplier or contract. The documents-only
    reader still has the policy the case cites, so a SQL value printed there is not
    database-only."""
    _, _, slots, _, v = _first_valid(data, "C.exception_threshold_now", "C", None)
    assert "supplier_id" not in slots and "contract_id" not in slots
    gold = copy.deepcopy(v["gold"])
    printed = gold["facts"][0]["value"]
    gold["facts"].append({"fact_id": "from_db", "source": "sql", "kind": "money",
                          "value": printed, "tolerance": "0", "uses": {}})
    gold["answer_requirement"] = [*gold["answer_requirement"], "from_db"]
    assert necessity(data, slots, gold)["docs_alone_sufficient"] is True
    gold["facts"][-1]["value"] = printed + Decimal("0.37")  # printed nowhere
    assert necessity(data, slots, gold) | {"sql_alone_lacks": []} == {
        "sql_alone_sufficient": False, "docs_alone_sufficient": False,
        "sql_alone_lacks": [], "docs_alone_lack": ["from_db"]}


def test_red_arm_permitted_group_member_must_see_the_complete_answer(
        data: InstanceData, plan: list[dict[str, Any]]) -> None:
    pair = {s["group_member"]: s for s in plan if s["group_id"] == "G-0001"}
    honest = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
    x, _ = honest.fill_group(pair["permitted"], pair["denied"], 0)

    partial = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
    real = partial._validated
    tampered: list[str] = []

    def validated(t: Template, slots: dict[str, Any], pid: str, cls: str,
                  want_ool: bool | None, need_probe: bool = False) -> dict[str, Any] | None:
        v = real(t, slots, pid, cls, want_ool, need_probe)
        if v is not None and not tampered:  # the first acceptable principal sees only part
            tampered.append(pid)
            v["restricted_probe"] = [{"fact_id": "x", "authorized_value": 1,
                                      "unauthorized_value": 2}]
        return v

    partial._validated = validated  # type: ignore[method-assign]
    x2, _ = partial.fill_group(pair["permitted"], pair["denied"], 0)
    assert tampered == [x["principal_id"]]
    assert (x2["family_id"], x2["principal_id"]) != (x["family_id"], x["principal_id"])
    assert x2["restricted_probe"] is None


def test_red_arm_group_does_not_reuse_a_family(data: InstanceData,
                                               plan: list[dict[str, Any]]) -> None:
    pair = {s["group_member"]: s for s in plan if s["group_id"] == "G-0001"}
    first, _ = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL).fill_group(
        pair["permitted"], pair["denied"], 0)
    b = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
    b.families.add(first["family_id"])
    again, denied = b.fill_group(pair["permitted"], pair["denied"], 0)
    assert again["family_id"] != first["family_id"]
    assert denied["family_id"] == again["family_id"]


def test_red_arm_uncited_contract_document_is_searched(data: InstanceData) -> None:
    """A SQL-only question cites no document. A schedule that names its contract but not
    the supplier is reached only through the supplier's contracts."""
    _, _, slots, _, v = _first_valid(data, "S.raw_otd", "S", None)
    cid = min(c["contract_id"] for c in data.tables["contracts"]
              if c["supplier_id"] == slots["supplier_id"])
    schedule = {"rendered": f"Schedule to contract {cid}. Agreed figure: 73,519.42."}
    assert slots["supplier_id"] not in schedule["rendered"]
    extended = dataclasses.replace(data, docs={**data.docs, ("DOC-TEST-SCHEDULE", 1): schedule})
    gold = copy.deepcopy(v["gold"])
    assert not any(f["source"] == "doc" for f in gold["facts"])
    gold["facts"][0]["value"] = Decimal("73519.42")
    assert necessity(data, slots, gold)["docs_alone_sufficient"] is False
    assert necessity(extended, slots, gold)["docs_alone_sufficient"] is True


# ------------------------------------------------------------------ restricted-probe slots
def _slot(cid: str, split: str, cls: str, *, ool: bool = False, injection: bool = False,
          group: str | None = None) -> dict[str, Any]:
    return {"case_id": cid, "split": split, "class": cls, "group_id": group,
            "group_member": "permitted" if group else None,
            "overlays": {"injection": injection, "ool": ool}}


def test_probe_slots_take_the_share_of_every_split_over_the_strata() -> None:
    plan = [_slot(f"{sp}-{cls}-{ool}-{i}", sp, cls, ool=ool)
            for sp in ("dev", "test") for cls in ("S", "X") for ool in (False, True)
            for i in range(49)]
    plan += [_slot(f"test-D-{i}", "test", "D") for i in range(100)]
    chosen = probe_slots(plan, SEED, (1, 10))
    by_id = {s["case_id"]: s for s in plan}
    for split, size, tenth in (("dev", 196, 20), ("test", 296, 30)):  # rounded up
        assert sum(1 for s in plan if s["split"] == split) == size
        mine = [by_id[c] for c in chosen if by_id[c]["split"] == split]
        assert len(mine) == tenth
        strata = Counter(probe_stratum(s) for s in mine)
        need = len(mine)
        assert strata["sql"] == need * dict(PROBE_STRATA)["sql"] // 100
        assert strata["cross_source_out_of_layer"] == (
            need * dict(PROBE_STRATA)["cross_source_out_of_layer"] // 100)
        assert sum(strata.values()) == need and None not in strata
    assert probe_slots(plan, SEED, (1, 10)) == chosen
    assert probe_slots(plan, SEED + 1, (1, 10)) != chosen


def test_probe_slots_exclude_groups_injection_and_other_classes() -> None:
    assert probe_stratum(_slot("a", "test", "X", group="G-1")) is None
    assert probe_stratum(_slot("b", "test", "X", injection=True)) is None
    assert probe_stratum(_slot("c", "test", "S", injection=True)) is None
    assert probe_stratum(_slot("d", "test", "D")) is None
    assert probe_stratum(_slot("e", "test", "S")) == "sql"
    assert probe_stratum(_slot("f", "test", "X", ool=True)) == "cross_source_out_of_layer"
    assert probe_stratum(_slot("g", "test", "X")) == "cross_source_in_layer"


def test_every_probe_slot_is_bound_to_a_probe_case(plan: list[dict[str, Any]],
                                                   cases: list[dict[str, Any]]) -> None:
    want = probe_slots(plan, SEED, PROBE_SHARE_ON_SMALL)
    assert want
    for c in cases:
        if c["case_id"] in want:
            assert c["restricted_probe"], c["case_id"]


def test_probe_requirement_is_checked_before_acceptance(data: InstanceData,
                                                        plan: list[dict[str, Any]]) -> None:
    """A slot that needs a probe never takes a principal who sees every row; the candidates
    it passes over stay available to ordinary slots (the probe walk has its own cursor).

    Checked on every template compatible with an S and an X probe slot. A template counts
    only where some candidate before the probe's is still free afterwards, so a shared
    cursor would have skipped it; at least one such template must exist."""
    discriminating = 0
    for stratum in ("sql", "cross_source_in_layer"):
        slot = next(s for s in plan if probe_stratum(s) == stratum)
        for t in CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)._compatible(slot):
            def builder(t: Template = t) -> CaseBuilder:
                b = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
                b._compatible = lambda _s: [t]  # type: ignore[method-assign]
                return b

            fresh = builder()
            try:
                probed = fresh.fill_single(slot, 0, probe=True)
            except RuntimeError:
                continue  # no partially-sighted principal for this template on the fixture
            assert probed["restricted_probe"]
            after = fresh.fill_single(slot, 0, probe=False)
            ref = builder()  # an ordinary walk from the start, probe's family taken
            ref.families.add(probed["family_id"])
            expected = ref.fill_single(slot, 0, probe=False)
            assert (after["family_id"], after["principal_id"]) == (
                expected["family_id"], expected["principal_id"]), t.id
            cands = [(family_id(t, sl), pid) for sl, pid, _ in fresh.candidates(t, False)]
            if cands.index((expected["family_id"], expected["principal_id"])) < cands.index(
                    (probed["family_id"], probed["principal_id"])):
                discriminating += 1
    assert discriminating >= 1


# ------------------------------------------------------------------ template spread
def test_least_used_template_is_tried_first(data: InstanceData) -> None:
    b = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
    ts = [t for t in TEMPLATES if t.cls == "X"][:4]
    assert [t.id for t in b._least_used_first(ts, 0)] == [t.id for t in ts]
    assert [t.id for t in b._least_used_first(ts, 1)] == [t.id for t in ts[1:] + ts[:1]]
    b.template_use[ts[1].id] = 2
    b.template_use[ts[2].id] = 1
    assert [t.id for t in b._least_used_first(ts, 1)] == [ts[3].id, ts[0].id, ts[2].id,
                                                          ts[1].id]


def test_template_use_counts_every_bound_family(cases: list[dict[str, Any]], data: InstanceData,
                                              plan: list[dict[str, Any]]) -> None:
    b = CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL)
    built = b.build(plan)
    # One count per question: the two members of a counterfactual pair count once.
    assert b.template_use == Counter(
        t for t in {c["family_id"]: c["template_id"] for c in built}.values())


# ------------------------------------------------------------------ parameterised SQL facts
def test_red_arm_sql_fact_parameterised_by_a_document_needs_the_document(
        data: InstanceData) -> None:
    """The SQL fact takes its threshold from a document: SQL alone cannot compute it. With
    the dependency removed, the same case passes as SQL-answerable and would be rejected
    as cross-source."""
    _, _, slots, _, v = _first_valid(data, "X.exception_required_value", "X", False)
    gold = v["gold"]
    assert necessity(data, slots, gold)["sql_alone_sufficient"] is False
    stripped = copy.deepcopy(gold)
    for f in stripped["facts"]:
        f.pop("depends_on", None)
    assert necessity(data, slots, stripped)["sql_alone_sufficient"] is True


# ------------------------------------------------------------------ row index
def test_row_index_is_per_principal_and_complete(data: InstanceData) -> None:
    full = data.view("fin_ctrl")
    part = data.view("buyer_in")
    for v in (full, part):
        idx = v.by("invoices", "supplier_id")
        assert sorted(r["invoice_id"] for rows in idx.values() for r in rows) == sorted(
            r["invoice_id"] for r in v.rows("invoices"))
        assert all(r["supplier_id"] == k for k, rows in idx.items() for r in rows)
    assert sum(map(len, part.by("invoices", "supplier_id").values())) < sum(
        map(len, full.by("invoices", "supplier_id").values()))
