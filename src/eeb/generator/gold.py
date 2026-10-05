"""Scenario gold facts with provenance (spec §6.2, expected-answer provenance).

Values are derived in Python from the final generated rows and documents, never from the
planting parameters alone, so random background data that happens to fall inside a
scenario's definition is counted. Each SQL-sourced fact also carries ``gold_sql``. The
Postgres tests execute it and compare, which gives a second, independent derivation.
Doc-sourced facts carry the exact phrase; the writer resolves it to a character span.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from eeb.generator.core import GENERATOR_VERSION, Instance, money, quarter_bounds
from eeb.generator.documents import (
    phrase_cap,
    phrase_credit_rate,
    phrase_indexation,
    phrase_indexation_date,
    phrase_otd_target,
    phrase_surcharge,
    phrase_threshold,
)
from eeb.generator.domain import (
    POLICY_V2_EFFECTIVE,
    S1_QUARTERS,
    S3_QUARTER,
    THRESHOLDS_V1,
    THRESHOLDS_V2,
    World,
)


def half_up(x: Decimal, places: int) -> Decimal:
    """Postgres ``round(numeric, n)`` rounds half away from zero; mirror it."""
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


class Gold:
    def __init__(self, inst: Instance, w: World) -> None:
        self.inst, self.w = inst, w
        t = inst.tables
        self.pos = {p["po_id"]: p for p in t["purchase_orders"]}
        self.lines = t["po_lines"]
        self.rec = {(r["po_id"], r["line_no"]): r for r in t["goods_receipts"]}
        self.facts: list[dict[str, Any]] = []

    def prov(self, scenario_id: str, kind: str) -> dict[str, Any]:
        return {"generator_version": GENERATOR_VERSION, "seed": self.inst.config.seed,
                "scenario_id": scenario_id, "scenario_kind": kind,
                "derivation": "python-from-final-rows"}

    def fact(self, scenario_id: str, kind: str, name: str, value: Any, *, fact_kind: str,
             source: str, unit: str | None = None, tolerance: str = "0", **extra: Any) -> str:
        fid = f"{scenario_id}.{name}"
        self.facts.append({"fact_id": fid, "scenario_id": scenario_id, "kind": fact_kind,
                           "value": value, "unit": unit, "tolerance": tolerance,
                           "source": source, "provenance": self.prov(scenario_id, kind),
                           "exposure_sensitive": False, **extra})
        return fid

    # ------------------------------------------------------------------ S1 indexation
    def s1(self) -> None:
        for sc in self.w.s1:
            sid, cid = sc["scenario_id"], sc["contract_id"]
            doc = {"doc_id": f"DOC-MSA-{cid}", "version": 2}
            c = self.w.contracts[cid]
            pct = self.fact(sid, "S1", "indexation_pct", sc["pct"], fact_kind="number",
                            source="doc", unit="percent",
                            doc_ref={**doc, "phrase": phrase_indexation(sc["pct"])})
            self.fact(sid, "S1", "indexation_effective", sc["date"], fact_kind="date",
                      source="doc", doc_ref={**doc, "phrase": phrase_indexation_date(sc["date"])})
            self.fact(sid, "S1", "expedite_surcharge_pct", c.surcharge_pct, fact_kind="number",
                      source="doc", unit="percent",
                      doc_ref={**doc, "phrase": phrase_surcharge(c.surcharge_pct)})
            for item in sc["items"]:
                cost: dict[str, str] = {}
                for quarter in S1_QUARTERS:
                    first, last = quarter_bounds(quarter)
                    sel = [ln for ln in self.lines if ln["item_id"] == item
                           and self.pos[ln["po_id"]]["supplier_id"] == sc["supplier_id"]
                           and first <= self.pos[ln["po_id"]]["order_date"] <= last]
                    qty = sum(ln["qty"] for ln in sel)
                    val = half_up(sum((ln["qty"] * ln["unit_price"] for ln in sel), Decimal(0))
                                  / qty, 4)
                    cost[quarter] = self.fact(
                        sid, "S1", f"{item}.effective_unit_cost.{quarter}", val,
                        fact_kind="money", source="sql",
                        unit=self.w.supplier_currency[sc["supplier_id"]], tolerance="0.0001",
                        gold_sql=(
                            "SELECT round(sum(l.qty * l.unit_price) / sum(l.qty), 4) "
                            "FROM eeb.po_lines l JOIN eeb.purchase_orders p ON p.po_id = l.po_id "
                            f"WHERE p.supplier_id = '{sc['supplier_id']}' AND l.item_id = '{item}' "
                            f"AND p.order_date BETWEEN '{first}' AND '{last}'"))
                q2, q3 = (next(f["value"] for f in self.facts if f["fact_id"] == cost[x])
                          for x in S1_QUARTERS)
                self.fact(sid, "S1", f"{item}.effective_unit_cost_change_pct",
                          half_up((q3 - q2) / q2 * 100, 2), fact_kind="number", source="derived",
                          unit="percent", tolerance="0.01",
                          derived={"op": "pct_change", "inputs": [cost[S1_QUARTERS[0]],
                                                                   cost[S1_QUARTERS[1]]]})
            _ = pct

    # ------------------------------------------------------------------ S2 SLA credits
    def s2(self) -> None:
        for sc in self.w.s2:
            sid, cid = sc["scenario_id"], sc["contract_id"]
            c = self.w.contracts[cid]
            doc = {"doc_id": f"DOC-SLA-{cid}", "version": 1}
            t = self.fact(sid, "S2", "otd_target_pct", c.sla_threshold_pct, fact_kind="number",
                          source="doc", unit="percent",
                          doc_ref={**doc, "phrase": phrase_otd_target(c.sla_threshold_pct)})
            rate = self.fact(sid, "S2", "credit_pct_per_point", c.sla_credit_pct_per_point,
                             fact_kind="number", source="doc", unit="percent",
                             doc_ref={**doc, "phrase": phrase_credit_rate(
                                 c.sla_credit_pct_per_point)})
            cap = self.fact(sid, "S2", "credit_cap_pct", c.sla_cap_pct, fact_kind="number",
                            source="doc", unit="percent",
                            doc_ref={**doc, "phrase": phrase_cap(c.sla_cap_pct)})
            first, last = quarter_bounds(sc["quarter"])
            sel = [ln for ln in self.lines
                   if self.pos[ln["po_id"]]["supplier_id"] == sc["supplier_id"]
                   and first <= ln["promised_date"] <= last]
            n = len(sel)
            ontime = sum(1 for ln in sel if (r := self.rec.get((ln["po_id"], ln["line_no"])))
                         is not None and r["received_date"] <= ln["promised_date"])
            excused = sum(1 for ln in sel if (r := self.rec.get((ln["po_id"], ln["line_no"])))
                          is not None and r["received_date"] > ln["promised_date"]
                          and r["delay_cause"] in ("force_majeure", "buyer"))
            base = money(sum((ln["qty"] * ln["unit_price"] for ln in sel), Decimal(0)))
            raw = half_up(Decimal(100 * ontime) / n, 2)
            adj = half_up(Decimal(100 * (ontime + excused)) / n, 2)
            where = (f"p.supplier_id = '{sc['supplier_id']}' "
                     f"AND l.promised_date BETWEEN '{first}' AND '{last}'")
            frm = ("FROM eeb.po_lines l JOIN eeb.purchase_orders p ON p.po_id = l.po_id "
                   "LEFT JOIN eeb.goods_receipts r ON r.po_id = l.po_id AND r.line_no = l.line_no ")
            self.fact(sid, "S2", "lines_measured", n, fact_kind="number", source="sql",
                      gold_sql=f"SELECT count(*) {frm}WHERE {where}")
            self.fact(sid, "S2", "raw_otd_pct", raw, fact_kind="number", source="sql",
                      unit="percent", tolerance="0.01",
                      gold_sql=("SELECT round(100.0 * count(*) FILTER (WHERE r.received_date <= "
                                f"l.promised_date) / count(*), 2) {frm}WHERE {where}"))
            adj_id = self.fact(
                sid, "S2", "adjusted_otd_pct", adj, fact_kind="number", source="sql",
                unit="percent", tolerance="0.01",
                gold_sql=("SELECT round(100.0 * count(*) FILTER (WHERE r.received_date <= "
                          "l.promised_date OR r.delay_cause IN ('force_majeure', 'buyer')) / "
                          f"count(*), 2) {frm}WHERE {where}"))
            base_id = self.fact(
                sid, "S2", "credit_base", base, fact_kind="money", source="sql",
                unit=self.w.supplier_currency[sc["supplier_id"]], tolerance="0.01",
                gold_sql=f"SELECT round(sum(l.qty * l.unit_price), 2) {frm}WHERE {where}")
            points = max(0, math.floor(c.sla_threshold_pct - adj))
            pts_id = self.fact(sid, "S2", "shortfall_points", points, fact_kind="number",
                               source="derived",
                               derived={"op": "floor_shortfall", "inputs": [t, adj_id]})
            credit_pct = min(points * c.sla_credit_pct_per_point, c.sla_cap_pct)
            pct_id = self.fact(sid, "S2", "credit_pct", credit_pct, fact_kind="number",
                               source="derived", unit="percent",
                               derived={"op": "min_mul_cap", "inputs": [pts_id, rate, cap]})
            self.fact(sid, "S2", "credit_amount", money(base * credit_pct / 100),
                      fact_kind="money", source="derived",
                      unit=self.w.supplier_currency[sc["supplier_id"]], tolerance="0.01",
                      derived={"op": "pct_of", "inputs": [pct_id, base_id]})
            filed = any(cl["contract_id"] == cid and cl["quarter"] == sc["quarter"]
                        for cl in self.inst.tables["service_credit_claims"])
            self.fact(sid, "S2", "claim_filed", filed, fact_kind="boolean", source="sql",
                      gold_sql=("SELECT count(*) > 0 FROM eeb.service_credit_claims WHERE "
                                f"contract_id = '{cid}' AND quarter = '{sc['quarter']}'"))

    # ------------------------------------------------------------------ S3 thresholds
    def s3(self) -> None:
        sid = f"S3-{S3_QUARTER}"
        first, last = quarter_bounds(S3_QUARTER)
        assert first >= POLICY_V2_EFFECTIVE
        for cur in ("INR", "EUR", "GBP"):
            self.fact(sid, "S3", f"threshold_{cur}", THRESHOLDS_V2[cur], fact_kind="money",
                      source="doc", unit=cur,
                      doc_ref={"doc_id": "DOC-POL-PROC", "version": 2,
                               "phrase": phrase_threshold(cur, THRESHOLDS_V2)})
        totals: dict[str, Decimal] = {}
        for ln in self.lines:
            totals[ln["po_id"]] = totals.get(ln["po_id"], Decimal(0)) + ln["qty"] * ln["unit_price"]
        exc = {e["po_id"]: e["amount"] for e in self.inst.tables["policy_exceptions"]}

        def noncompliant(th: dict[str, Decimal]) -> list[str]:
            return sorted(
                p["po_id"] for p in self.pos.values()
                if p["contract_id"] is None and first <= p["order_date"] <= last
                and totals[p["po_id"]] > th[p["currency"]]
                and (p["po_id"] not in exc or exc[p["po_id"]] < totals[p["po_id"]]))

        def sql(th: dict[str, Decimal]) -> str:
            case = " ".join(f"WHEN '{c}' THEN {th[c]}" for c in ("INR", "EUR", "GBP"))
            return ("SELECT p.po_id FROM eeb.purchase_orders p JOIN (SELECT po_id, "
                    "sum(qty * unit_price) AS total FROM eeb.po_lines GROUP BY po_id) s "
                    "ON s.po_id = p.po_id LEFT JOIN eeb.policy_exceptions e ON e.po_id = p.po_id "
                    f"WHERE p.contract_id IS NULL AND p.order_date BETWEEN '{first}' AND '{last}' "
                    f"AND s.total > CASE p.currency {case} END "
                    "AND (e.exception_id IS NULL OR e.amount < s.total) ORDER BY p.po_id")

        current = noncompliant(THRESHOLDS_V2)
        self.fact(sid, "S3", "noncompliant_po_ids", current, fact_kind="entity_set",
                  source="sql", gold_sql=sql(THRESHOLDS_V2))
        self.fact(sid, "S3", "noncompliant_po_ids_under_superseded_v1",
                  noncompliant(THRESHOLDS_V1), fact_kind="entity_set", source="sql",
                  gold_sql=sql(THRESHOLDS_V1), note="stale-policy answer; not the correct answer")

    def build(self) -> list[dict[str, Any]]:
        self.s1()
        self.s2()
        self.s3()
        return self.facts
