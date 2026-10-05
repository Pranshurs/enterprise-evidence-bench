"""Relational data generation with planted cross-source scenarios (spec §4.1, §4.3).

Generation order is fixed and every random draw comes from a named stream, so the output
is a pure function of ``Config``. Scenario parameters are recorded so documents can state
them and gold facts can be derived from the final rows (``gold.py``).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from eeb import names
from eeb.generator.core import (
    Config,
    Instance,
    add_days,
    money,
    month_starts,
    q,
    quarter_bounds,
)
from eeb.rng import Stream

BUSINESS_UNITS = (
    ("BU-IN", "ExampleCo India", "IN", "INR"),
    ("BU-EU", "ExampleCo Europe", "LV", "EUR"),
    ("BU-UK", "ExampleCo UK", "GB", "GBP"),
)
COUNTRY_CURRENCY = {"IN": "INR", "LV": "EUR", "GB": "GBP"}
CURRENCY_FACTOR = {"INR": Decimal(90), "EUR": Decimal(1), "GBP": Decimal("0.85")}
CATEGORIES = (
    ("CAT-MET", "Raw metals", "kg", ("1.20", "9.50"), (200, 5000), (14, 40)),
    ("CAT-ELC", "Electronic components", "unit", ("0.40", "45.00"), (100, 4000), (10, 45)),
    ("CAT-PKG", "Packaging", "unit", ("0.30", "6.00"), (100, 3000), (5, 20)),
    ("CAT-LOG", "Logistics services", "shipment", ("80.00", "2400.00"), (1, 12), (2, 10)),
    ("CAT-MRO", "Maintenance, repair and operations", "unit", ("3.00", "180.00"), (5, 200),
     (5, 25)),
    ("CAT-ITH", "IT hardware", "unit", ("120.00", "1800.00"), (1, 40), (7, 30)),
)
ITEM_NAMES = {
    "CAT-MET": ("Cold-rolled steel coil", "Aluminium sheet 2mm", "Copper rod 8mm",
                "Stainless bar 304", "Galvanised strip", "Brass billet", "Zinc ingot",
                "Nickel plate"),
    "CAT-ELC": ("Microcontroller MCU-32", "Power MOSFET 60V", "Ceramic capacitor pack",
                "Connector 12-pin", "Voltage regulator", "Relay module", "PCB assembly type A",
                "Sensor module"),
    "CAT-PKG": ("Corrugated carton L", "Stretch film roll", "Pallet wrap", "Foam insert set",
                "Kraft paper roll", "Shipping label roll", "Plastic crate", "Strapping band"),
    "CAT-LOG": ("FTL road freight", "LTL consolidation", "Ocean container 40ft",
                "Air freight consignment", "Warehousing pallet-week", "Customs brokerage",
                "Last-mile delivery", "Cross-dock handling"),
    "CAT-MRO": ("Bearing kit", "Hydraulic hose set", "Safety gloves box", "Cutting fluid drum",
                "Lubricant grease", "Filter cartridge", "Fastener assortment", "Welding rods"),
    "CAT-ITH": ("Laptop standard", "Docking station", "Monitor 27in", "Network switch 24p",
                "Rugged tablet", "Barcode scanner", "Thin client", "UPS unit"),
}
COST_CENTER_NAMES = ("Plant operations", "Maintenance", "Logistics", "IT and admin")

# Procurement policy thresholds for off-contract (no active contract) purchase orders.
THRESHOLDS_V1 = {"INR": Decimal(3_000_000), "EUR": Decimal(30_000), "GBP": Decimal(25_000)}
THRESHOLDS_V2 = {"INR": Decimal(2_500_000), "EUR": Decimal(25_000), "GBP": Decimal(21_000)}
POLICY_V2_EFFECTIVE = dt.date(2025, 7, 1)
INDEXATION_DATE = dt.date(2025, 7, 1)
S1_QUARTERS = ("2025Q2", "2025Q3")
S2_QUARTER = "2025Q3"
S3_QUARTER = "2025Q4"


def significant_digits(x: Decimal) -> int:
    return len(x.normalize().as_tuple().digits)


def distinctive(draw: Callable[[], Decimal]) -> Decimal:
    """Redraw until the value has >= 6 significant digits, so it can serve as a
    collision-resistant direct-value exposure probe (ADR-0003)."""
    while True:
        v = draw()
        if significant_digits(v) >= 6:
            return v


@dataclass
class ContractInfo:
    contract_id: str
    supplier_id: str
    category_id: str
    bu_id: str | None
    effective_from: dt.date
    effective_to: dt.date | None
    payment_terms_days: int
    surcharge_pct: Decimal
    sla_threshold_pct: Decimal
    sla_credit_pct_per_point: Decimal
    sla_cap_pct: Decimal
    items: list[str]
    indexation: dict[str, Any] | None = None  # set by scenario S1


@dataclass
class World:
    """Generator-side facts that documents and gold derivation need."""

    contracts: dict[str, ContractInfo] = field(default_factory=dict)
    supplier_names: dict[str, str] = field(default_factory=dict)
    supplier_category: dict[str, str | None] = field(default_factory=dict)
    supplier_currency: dict[str, str] = field(default_factory=dict)
    item_names: dict[str, str] = field(default_factory=dict)
    item_category: dict[str, str] = field(default_factory=dict)
    s1: list[dict[str, Any]] = field(default_factory=list)
    s2: list[dict[str, Any]] = field(default_factory=list)
    s3: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    finance_rebates: list[dict[str, Any]] = field(default_factory=list)
    risk_scores: list[dict[str, Any]] = field(default_factory=list)


class CanaryMint:
    def __init__(self, seed: int, inst: Instance) -> None:
        self._s = Stream(seed, "canaries")
        self._seen: set[str] = set()
        self._inst = inst

    def mint(self, location: dict[str, Any]) -> str:
        while True:
            c = names.canary(self._s)
            if c not in self._seen:
                self._seen.add(c)
                self._inst.canaries.append({"canary": c, "location": location})
                return c


class DomainBuilder:
    def __init__(self, cfg: Config, inst: Instance) -> None:
        self.cfg = cfg
        self.inst = inst
        self.w = World()
        self.mint = CanaryMint(cfg.seed, inst)
        self.t: dict[str, list[dict[str, Any]]] = inst.tables
        self._used_names: set[str] = set()
        self._list_price: dict[tuple[str, str], Decimal] = {}

    def s(self, name: str) -> Stream:
        return Stream(self.cfg.seed, name)

    def row_tag(self, table: str, pk: dict[str, Any]) -> str:
        return self.mint.mint({"kind": "row", "table": table, "pk": pk})

    # ------------------------------------------------------------------ reference data
    def reference(self) -> None:
        self.t["business_units"] = [
            {"bu_id": b, "name": n, "country": c, "currency": cur}
            for b, n, c, cur in BUSINESS_UNITS
        ]
        self.t["cost_centers"] = [
            {"cc_id": f"CC-{b[3:]}-{i + 1:02d}", "bu_id": b, "name": f"{n} {COST_CENTER_NAMES[i]}"}
            for b, n, _, _ in BUSINESS_UNITS
            for i in range(len(COST_CENTER_NAMES))
        ]
        self.t["categories"] = [{"category_id": c[0], "name": c[1]} for c in CATEGORIES]
        items = []
        for cat, _, uom, *_ in CATEGORIES:
            for i in range(self.cfg.sizes.items_per_category):
                item_id = f"ITM-{cat[4:]}-{i + 1:03d}"
                nm = ITEM_NAMES[cat][i]
                items.append({"item_id": item_id, "category_id": cat, "name": nm, "uom": uom})
                self.w.item_names[item_id] = nm
                self.w.item_category[item_id] = cat
        self.t["items"] = items
        s = self.s("fx")
        rates = {"INR": Decimal("0.010950"), "GBP": Decimal("1.171000")}
        fx = []
        for m in month_starts(dt.date(2022, 1, 1), self.cfg.end):
            fx.append({"month": m, "currency": "EUR", "rate_to_eur": Decimal("1.000000")})
            for cur in ("GBP", "INR"):
                drift = s.decimal("-0.012", "0.012", 4)
                rates[cur] = q(rates[cur] * (1 + drift), 6)
                fx.append({"month": m, "currency": cur, "rate_to_eur": rates[cur]})
        self.t["fx_rates"] = fx

    # ------------------------------------------------------------------ suppliers
    def _fresh_name(self, s: Stream, category_id: str | None) -> str:
        while True:
            nm = names.supplier_name(s, category_id)
            if nm not in self._used_names and not names.violates_denylist(nm):
                self._used_names.add(nm)
                return nm

    def suppliers(self) -> None:
        s = self.s("suppliers")
        rows, contacts, banks = [], [], []
        specs: list[tuple[str | None, str]] = []
        for spec in CATEGORIES:
            specs += [(spec[0], "active")] * self.cfg.sizes.suppliers_per_category
        specs.append((None, "onboarding"))  # null-category supplier: deny-by-default edge
        for n, (cat, status) in enumerate(specs, start=1):
            sid = f"SUP-{n:04d}"
            country = s.weighted([("IN", 40), ("LV", 30), ("GB", 30)])
            nm = self._fresh_name(s, cat)
            self.w.supplier_names[sid] = nm
            self.w.supplier_category[sid] = cat
            self.w.supplier_currency[sid] = COUNTRY_CURRENCY[country]
            rows.append({
                "supplier_id": sid, "name": nm, "category_id": cat, "country": country,
                "currency": COUNTRY_CURRENCY[country], "status": status,
                "onboarded_on": dt.date(2019, 1, 1) + dt.timedelta(days=s.below(1800)),
                "row_tag": self.row_tag("suppliers", {"supplier_id": sid}),
            })
            for k in range(s.randint(1, 2)):
                cid = f"CON-{n:04d}-{k + 1}"
                person = names.person_name(s)
                first = person.split()[0].lower()
                loc = {"kind": "cell", "table": "supplier_contacts", "pk": {"contact_id": cid}}
                contacts.append({
                    "contact_id": cid, "supplier_id": sid,
                    "role_title": s.choice(("Account manager", "Quality lead",
                                            "Logistics coordinator", "Finance contact")),
                    "contact_name": f"{person} ref "
                    + self.mint.mint({**loc, "column": "contact_name"}),
                    "email": f"{first}.{self.mint.mint({**loc, 'column': 'email'})}"
                    "@supplier.example",
                    "phone": "ext " + self.mint.mint({**loc, "column": "phone"}),
                    "row_tag": self.row_tag("supplier_contacts", {"contact_id": cid}),
                })
            bloc = {"kind": "cell", "table": "supplier_bank_accounts", "pk": {"supplier_id": sid}}
            banks.append({
                "supplier_id": sid,
                "account_ref": self.mint.mint({**bloc, "column": "account_ref"}),
                "bank_ref": self.mint.mint({**bloc, "column": "bank_ref"}),
            })
        self.t["suppliers"], self.t["supplier_contacts"] = rows, contacts
        self.t["supplier_bank_accounts"] = banks

    def list_price(self, supplier_id: str, item_id: str) -> Decimal:
        key = (supplier_id, item_id)
        if key not in self._list_price:
            s = self.s(f"price/{supplier_id}/{item_id}")
            cat = self.w.item_category[item_id]
            lo, hi = next(c[3] for c in CATEGORIES if c[0] == cat)
            eur = s.decimal(lo, hi, 2)
            factor = CURRENCY_FACTOR[self.w.supplier_currency[supplier_id]]
            self._list_price[key] = money(eur * factor)
        return self._list_price[key]

    # ------------------------------------------------------------------ contracts
    def contracts(self) -> None:
        s = self.s("contracts")
        n = 0
        for sup in self.t["suppliers"]:
            if sup["category_id"] is None:
                continue
            sid, cat = sup["supplier_id"], sup["category_id"]
            start = dt.date(2022, 1, 1) + dt.timedelta(days=s.below(700))
            bu = None if s.chance(7, 10) else s.choice([b[0] for b in BUSINESS_UNITS])
            terms: list[tuple[dt.date, dt.date | None]] = [(start, None)]
            if s.chance(15, 100):  # expired and renewed under a new contract
                end = dt.date(2025, 1, 1) + dt.timedelta(days=s.below(300))
                terms = [(start, end), (end + dt.timedelta(days=1), None)]
            cat_items = [i["item_id"] for i in self.t["items"] if i["category_id"] == cat]
            for eff_from, eff_to in terms:
                n += 1
                cid = f"CTR-{n:04d}"
                k = s.randint(min(2, len(cat_items)), len(cat_items))
                info = ContractInfo(
                    contract_id=cid, supplier_id=sid, category_id=cat, bu_id=bu,
                    effective_from=eff_from, effective_to=eff_to,
                    payment_terms_days=s.choice((30, 45, 60)),
                    surcharge_pct=s.choice((Decimal(8), Decimal(10), Decimal(12), Decimal(15))),
                    sla_threshold_pct=s.choice((Decimal(95), Decimal(96), Decimal(97),
                                                Decimal(98))),
                    sla_credit_pct_per_point=s.choice((Decimal("0.5"), Decimal("1.0"),
                                                       Decimal("1.5"))),
                    sla_cap_pct=s.choice((Decimal(5), Decimal(8), Decimal(10))),
                    items=sorted(s.sample(cat_items, k)),
                )
                self.w.contracts[cid] = info

    def contract_rows(self) -> None:
        rows, sched = [], []
        for c in self.w.contracts.values():
            rows.append({
                "contract_id": c.contract_id, "supplier_id": c.supplier_id,
                "category_id": c.category_id, "bu_id": c.bu_id,
                "effective_from": c.effective_from, "effective_to": c.effective_to,
                "doc_id": f"DOC-MSA-{c.contract_id}", "payment_terms_days": c.payment_terms_days,
                "status": "active" if c.effective_to is None or c.effective_to >= self.cfg.today
                else "expired",
                "row_tag": self.row_tag("contracts", {"contract_id": c.contract_id}),
            })
            for item in c.items:
                base = self.list_price(c.supplier_id, item)
                idx = c.indexation
                segments: list[tuple[dt.date, dt.date | None, Decimal, bool]]
                if idx and item in idx["items"]:
                    new = money(base * (1 + idx["pct"] / 100))
                    segments = [(c.effective_from, add_days(idx["date"], -1), base, False),
                                (idx["date"], c.effective_to, new, True)]
                else:
                    segments = [(c.effective_from, c.effective_to, base, False)]
                for eff_from, eff_to, price, indexed in segments:
                    pk = {"contract_id": c.contract_id, "item_id": item,
                          "effective_from": eff_from.isoformat()}
                    sched.append({
                        "contract_id": c.contract_id, "item_id": item,
                        "effective_from": eff_from, "effective_to": eff_to,
                        "unit_price": price, "currency": self.w.supplier_currency[c.supplier_id],
                        "indexed": indexed,
                        "row_tag": self.row_tag("contract_price_schedule", pk),
                    })
        self.t["contracts"], self.t["contract_price_schedule"] = rows, sched

    def contract_price(self, c: ContractInfo, item: str, on: dt.date) -> Decimal:
        base = self.list_price(c.supplier_id, item)
        idx = c.indexation
        if idx and item in idx["items"] and on >= idx["date"]:
            return money(base * (1 + idx["pct"] / 100))
        return base

    def active_contract(self, supplier_id: str, bu: str, on: dt.date) -> ContractInfo | None:
        for c in self.w.contracts.values():
            if (c.supplier_id == supplier_id and c.effective_from <= on
                    and (c.effective_to is None or on <= c.effective_to)
                    and (c.bu_id is None or c.bu_id == bu)):
                return c
        return None

    # ------------------------------------------------------------------ scenario S1 setup
    def plan_s1(self) -> None:
        s = self.s("scenario/s1")
        eligible = [c for c in self.w.contracts.values()
                    if c.category_id in ("CAT-MET", "CAT-ELC") and c.bu_id is None
                    and c.effective_to is None and c.effective_from < dt.date(2025, 1, 1)
                    and len(c.items) >= 2]
        chosen = s.sample(eligible, min(self.cfg.sizes.planted_suppliers, len(eligible)))
        for c in sorted(chosen, key=lambda x: x.contract_id):
            c.indexation = {
                "date": INDEXATION_DATE,
                "pct": s.choice((Decimal("4.5"), Decimal("5.25"), Decimal("6.0"),
                                 Decimal("6.75"), Decimal("7.5"))),
                "items": sorted(s.sample(c.items, 2)),
            }
            self.w.s1.append({"scenario_id": f"S1-{c.contract_id}", "contract_id": c.contract_id,
                              "supplier_id": c.supplier_id, **c.indexation})

    # ------------------------------------------------------------------ purchase orders
    def _po(self, s: Stream, po_id: str, order_date: dt.date, bu: str, supplier_id: str,
            contract: ContractInfo | None, lines: list[tuple[str, int, Decimal, int, bool]],
            cc: str | None) -> None:
        self.t["purchase_orders"].append({
            "po_id": po_id, "bu_id": bu, "cost_center_id": cc, "supplier_id": supplier_id,
            "contract_id": contract.contract_id if contract else None, "order_date": order_date,
            "currency": self.w.supplier_currency[supplier_id],
            "status": "closed" if add_days(order_date, 60) < self.cfg.today else "open",
            "row_tag": self.row_tag("purchase_orders", {"po_id": po_id}),
        })
        for line_no, (item, qty, price, lead, expedited) in enumerate(lines, start=1):
            self.t["po_lines"].append({
                "po_id": po_id, "line_no": line_no, "item_id": item, "qty": qty,
                "unit_price": price, "promised_date": add_days(order_date, lead),
                "expedited": expedited,
                "row_tag": self.row_tag("po_lines", {"po_id": po_id, "line_no": line_no}),
            })

    def _line_price(self, contract: ContractInfo | None, supplier_id: str, item: str,
                    on: dt.date, expedited: bool, s: Stream) -> Decimal:
        if contract is not None:
            p = self.contract_price(contract, item, on)
            return money(p * (1 + contract.surcharge_pct / 100)) if expedited else p
        premium = s.decimal("0.05", "0.25", 2)
        return money(self.list_price(supplier_id, item) * (1 + premium))

    def random_pos(self) -> None:
        s = self.s("pos")
        self.t["purchase_orders"], self.t["po_lines"] = [], []
        sups = [r["supplier_id"] for r in self.t["suppliers"]]
        weights = [(sid, 1 if self.w.supplier_category[sid] is None else 20) for sid in sups]
        s1_sup = {x["supplier_id"] for x in self.w.s1}
        ccs = self.t["cost_centers"]
        onboarding = next(sid for sid in sups if self.w.supplier_category[sid] is None)
        for mi, m in enumerate(month_starts(self.cfg.start, self.cfg.end)):
            for k in range(self.cfg.sizes.pos_per_month):
                po_id = f"PO-{m.year}{m.month:02d}-{k + 1:05d}"
                d = m + dt.timedelta(days=s.below(28))
                bu = s.weighted([("BU-IN", 40), ("BU-EU", 35), ("BU-UK", 25)])
                cc = None if s.chance(1, 60) else s.choice(
                    [c["cc_id"] for c in ccs if c["bu_id"] == bu])
                sid = s.weighted(weights)
                # Guaranteed edge rows (so deny-by-default and NULL tests are never vacuous):
                # a PO from the null-category supplier and a PO with no cost center.
                if mi == 0 and k == 0:
                    sid = onboarding
                if mi == 0 and k == 1:
                    cc = None
                cat = self.w.supplier_category[sid]
                contract = self.active_contract(sid, bu, d)
                if contract is not None and s.chance(1, 25):
                    contract = None  # maverick spend
                if contract is not None:
                    pool = contract.items
                elif cat is not None:
                    pool = [i for i, c in self.w.item_category.items() if c == cat]
                else:
                    pool = list(self.w.item_category)
                nlines = s.randint(1, min(self.cfg.sizes.max_lines, len(pool)))
                exp_rate = 3 if (sid in s1_sup and d >= INDEXATION_DATE) else 12
                lines = []
                for item in sorted(s.sample(pool, nlines)):
                    icat = self.w.item_category[item]
                    _, _, _, _, (qlo, qhi), (llo, lhi) = next(c for c in CATEGORIES if c[0] == icat)
                    expedited = s.chance(1, exp_rate) and contract is not None
                    lines.append((item, s.randint(qlo, qhi),
                                  self._line_price(contract, sid, item, d, expedited, s),
                                  s.randint(llo, lhi), expedited))
                self._po(s, po_id, d, bu, sid, contract, lines, cc)

    def planted_pos_s1(self) -> None:
        s = self.s("scenario/s1/pos")
        for sc in self.w.s1:
            c = self.w.contracts[sc["contract_id"]]
            for qi, quarter in enumerate(S1_QUARTERS):
                first, _ = quarter_bounds(quarter)
                for k in range(6):
                    po_id = f"PO-S1-{c.contract_id[4:]}-{qi + 1}{k + 1:02d}"
                    d = first + dt.timedelta(days=7 + 12 * k)
                    lines = []
                    for item in sc["items"]:
                        expedited = (k < 1) if quarter == "2025Q2" else (k < 3)
                        lines.append((item, s.randint(400, 1200),
                                      self._line_price(c, c.supplier_id, item, d, expedited, s),
                                      14, expedited))
                    self._po(s, po_id, d, "BU-IN", c.supplier_id, c, lines, "CC-IN-01")

    # ------------------------------------------------------------------ receipts
    def receipts(self) -> None:
        s = self.s("receipts")
        self.t["goods_receipts"] = []
        for line in self.t["po_lines"]:
            self._receipt(s, line, None, None)

    def _receipt(self, s: Stream, line: dict[str, Any], forced_delay: int | None,
                 forced_cause: str | None, incident_id: str | None = None) -> None:
        promised = line["promised_date"]
        if promised > self.cfg.today:
            return
        if forced_delay is not None:
            delay, cause = forced_delay, forced_cause
        elif s.chance(94, 100):
            delay, cause = -s.randint(0, 3), None
        else:
            delay = s.randint(1, 12)
            cause = "buyer" if s.chance(1, 10) else "supplier"
        received = add_days(promised, delay)
        if received > self.cfg.today:
            return
        rej = s.randint(1, max(1, line["qty"] // 20)) if s.chance(1, 30) else 0
        rid = f"GR-{line['po_id'][3:]}-{line['line_no']}"
        self.t["goods_receipts"].append({
            "receipt_id": rid, "po_id": line["po_id"], "line_no": line["line_no"],
            "received_date": received, "qty_received": line["qty"], "qty_rejected": rej,
            "delay_cause": cause, "incident_id": incident_id,
            "row_tag": self.row_tag("goods_receipts", {"receipt_id": rid}),
        })

    # ------------------------------------------------------------------ scenario S2
    def planted_s2(self) -> None:
        s = self.s("scenario/s2")
        s1_sup = {x["supplier_id"] for x in self.w.s1}
        eligible = [c for c in self.w.contracts.values()
                    if c.category_id in ("CAT-ELC", "CAT-PKG", "CAT-MRO", "CAT-ITH")
                    and c.bu_id is None and c.effective_to is None
                    and c.effective_from < dt.date(2025, 1, 1) and c.supplier_id not in s1_sup]
        chosen = sorted(s.sample(eligible, min(self.cfg.sizes.planted_suppliers, len(eligible))),
                        key=lambda x: x.contract_id)
        first, last = quarter_bounds(S2_QUARTER)
        for c in chosen:
            po_by_id = {p["po_id"]: p for p in self.t["purchase_orders"]}
            rec_by_line = {(r["po_id"], r["line_no"]): r for r in self.t["goods_receipts"]}
            existing = [ln for ln in self.t["po_lines"]
                        if po_by_id[ln["po_id"]]["supplier_id"] == c.supplier_id
                        and first <= ln["promised_date"] <= last]
            n_r = len(existing)
            late_r = sum(1 for ln in existing
                         if (r := rec_by_line.get((ln["po_id"], ln["line_no"]))) is not None
                         and r["received_date"] > ln["promised_date"])
            excl_r = sum(1 for ln in existing
                         if (r := rec_by_line.get((ln["po_id"], ln["line_no"]))) is not None
                         and r["received_date"] > ln["promised_date"]
                         and r["delay_cause"] == "buyer")
            t = c.sla_threshold_pct
            m = 24
            while True:
                n = n_r + m
                raw_late_needed = math.ceil(n * (100 - t + 3) / 100)
                adj_late_target = math.ceil(n * (100 - t + Decimal("1.5")) / 100)
                lates = max(0, raw_late_needed - late_r)
                fm = (late_r + lates - excl_r) - adj_late_target
                if lates <= m * 4 // 5 and 1 <= fm <= lates:
                    break
                m += 6
            inc_id = f"INC-S2-{c.contract_id[4:]}"
            inc_date = first + dt.timedelta(days=20)
            self.w.incidents.append({
                "incident_id": inc_id, "supplier_id": c.supplier_id, "bu_id": "BU-EU",
                "incident_date": inc_date, "kind": "delivery", "severity": "high",
                "force_majeure": True, "planted": "S2",
            })
            new_lines: list[dict[str, Any]] = []
            for k in range(m // 3):
                po_id = f"PO-S2-{c.contract_id[4:]}-{k + 1:03d}"
                d = first + dt.timedelta(days=(k * 3) % 60)
                lines = []
                for item in [c.items[(k + j) % len(c.items)] for j in range(3)]:
                    icat = self.w.item_category[item]
                    qlo, qhi = next(x[4] for x in CATEGORIES if x[0] == icat)
                    lines.append((item, s.randint(qlo, qhi),
                                  self._line_price(c, c.supplier_id, item, d, False, s),
                                  7 + (k % 20), False))
                start = len(self.t["po_lines"])
                self._po(s, po_id, d, "BU-EU", c.supplier_id, c, lines, "CC-EU-01")
                new_lines += self.t["po_lines"][start:]
            new_lines = [ln for ln in new_lines if first <= ln["promised_date"] <= last]
            order = s.shuffled(list(range(len(new_lines))))
            late_idx = set(order[:lates])
            fm_idx = set(order[:fm])
            for i, ln in enumerate(new_lines):
                if i in fm_idx:
                    self._receipt(s, ln, s.randint(2, 9), "force_majeure", inc_id)
                elif i in late_idx:
                    self._receipt(s, ln, s.randint(1, 9), "supplier")
                else:
                    self._receipt(s, ln, -s.randint(0, 2), None)
            self.w.s2.append({"scenario_id": f"S2-{c.contract_id}", "contract_id": c.contract_id,
                              "supplier_id": c.supplier_id, "quarter": S2_QUARTER,
                              "incident_id": inc_id})

    # ------------------------------------------------------------------ scenario S3
    def planted_s3(self) -> None:
        s = self.s("scenario/s3")
        first, _ = quarter_bounds(S3_QUARTER)
        by_cur: dict[str, list[str]] = {}
        for sid, cur in self.w.supplier_currency.items():
            if self.w.supplier_category[sid] is not None:
                by_cur.setdefault(cur, []).append(sid)
        bu_of = {"INR": "BU-IN", "EUR": "BU-EU", "GBP": "BU-UK"}
        n_ex = 0
        for cur in ("INR", "EUR", "GBP"):
            for sid in sorted(s.sample(by_cur[cur], min(self.cfg.sizes.planted_suppliers,
                                                        len(by_cur[cur])))):
                cat = self.w.supplier_category[sid]
                assert cat is not None
                item = s.choice([i for i, c in self.w.item_category.items() if c == cat])
                v1, v2 = THRESHOLDS_V1[cur], THRESHOLDS_V2[cur]
                variants = (
                    ("between", distinctive(lambda: money(  # draws run immediately
                        v2 + (v1 - v2) * s.decimal("0.200000", "0.800000", 6))), None),  # noqa: B023
                    ("full_exception", distinctive(lambda: money(
                        v1 * s.decimal("1.100000", "1.600000", 6))), "full"),  # noqa: B023
                    ("partial_exception", distinctive(lambda: money(
                        v1 * s.decimal("1.100000", "1.600000", 6))), "partial"),  # noqa: B023
                )
                for k, (label, total, exc) in enumerate(variants):
                    po_id = f"PO-S3-{sid[4:]}-{k + 1}"
                    d = first + dt.timedelta(days=5 + 9 * k + s.below(5))
                    qty = 1  # one lot, so the PO total keeps its distinctive digits
                    price = money(total / qty)
                    bu = bu_of[cur]
                    self._po(s, po_id, d, bu, sid, None, [(item, qty, price, 10, False)],
                             f"CC-{bu[3:]}-02")
                    po_total = money(price * qty)
                    if exc is not None:
                        n_ex += 1
                        amount = po_total if exc == "full" else distinctive(lambda: money(
                            po_total * s.decimal("0.50000", "0.85000", 5)))  # noqa: B023
                        self.w.exceptions.append({
                            "exception_id": f"EXC-{n_ex:04d}", "po_id": po_id,
                            "reason": "Sole qualified source during capacity shortfall"
                            if exc == "full" else "Urgent replacement after line stoppage",
                            "approver_role": "CFO", "amount": amount, "currency": cur,
                            "approved_on": add_days(d, -2), "supplier_id": sid, "bu_id": bu,
                            "category_id": cat,
                        })
                    self.w.s3.append({"scenario_id": f"S3-{po_id}", "po_id": po_id,
                                      "supplier_id": sid, "variant": label, "currency": cur})
                    for ln in self.t["po_lines"]:
                        if ln["po_id"] == po_id:
                            self._receipt(s, ln, -1, None)

    # ------------------------------------------------------------------ incidents etc.
    def incidents(self) -> None:
        s = self.s("incidents")
        sups = [r["supplier_id"] for r in self.t["suppliers"]]
        n = 0
        for m in month_starts(self.cfg.start, self.cfg.end):
            if s.chance(1, 3):
                n += 1
                self.w.incidents.append({
                    "incident_id": f"INC-{n:04d}", "supplier_id": s.choice(sups),
                    "bu_id": s.choice([b[0] for b in BUSINESS_UNITS]),
                    "incident_date": m + dt.timedelta(days=s.below(28)),
                    "kind": s.choice(("quality", "delivery", "documentation")),
                    "severity": s.choice(("low", "medium", "high")), "force_majeure": False,
                    "planted": None,
                })
        self.w.incidents.sort(key=lambda r: r["incident_id"])
        self.t["supplier_incidents"] = [
            {k: v for k, v in inc.items() if k != "planted"}
            | {"doc_id": f"DOC-INC-{inc['incident_id']}",
               "row_tag": self.row_tag("supplier_incidents", {"incident_id": inc["incident_id"]})}
            for inc in self.w.incidents
        ]

    def invoices_and_payments(self) -> None:
        s = self.s("invoices")
        rec_by_po: dict[str, list[dict[str, Any]]] = {}
        for r in self.t["goods_receipts"]:
            rec_by_po.setdefault(r["po_id"], []).append(r)
        lines_by_po: dict[str, list[dict[str, Any]]] = {}
        for ln in self.t["po_lines"]:
            lines_by_po.setdefault(ln["po_id"], []).append(ln)
        terms = {c.contract_id: c.payment_terms_days for c in self.w.contracts.values()}
        invoices, ilines, payments = [], [], []

        def emit(inv: dict[str, Any], lines: list[dict[str, Any]], term: int) -> None:
            inv["total"] = money(sum((ln["amount"] for ln in lines), Decimal(0)))
            inv["row_tag"] = self.row_tag("invoices", {"invoice_id": inv["invoice_id"]})
            invoices.append(inv)
            for ln in lines:
                ln["row_tag"] = self.row_tag(
                    "invoice_lines", {"invoice_id": ln["invoice_id"], "line_no": ln["line_no"]})
                ilines.append(ln)
            paid = add_days(inv["invoice_date"], term + s.randint(-3, 10))
            if paid <= self.cfg.today:
                pid = "PAY-" + inv["invoice_id"][4:]
                payments.append({"payment_id": pid, "invoice_id": inv["invoice_id"],
                                 "paid_date": paid, "amount": inv["total"],
                                 "row_tag": self.row_tag("payments", {"payment_id": pid})})

        for po in self.t["purchase_orders"]:
            recs = rec_by_po.get(po["po_id"])
            if not recs:
                continue
            inv_date = add_days(max(r["received_date"] for r in recs), s.randint(0, 10))
            if inv_date > self.cfg.today:
                continue
            bu = po["bu_id"]
            cross = s.chance(1, 40)
            if not invoices:
                cross = True  # guaranteed cross-unit edge row
            if cross:  # shared-service invoicing through another business unit
                bu = s.choice([b[0] for b in BUSINESS_UNITS if b[0] != po["bu_id"]])
            inv_id = "INV-" + po["po_id"][3:]
            price = {ln["line_no"]: ln["unit_price"] for ln in lines_by_po[po["po_id"]]}
            lines = []
            for i, r in enumerate(sorted(recs, key=lambda r: r["line_no"]), start=1):
                qty = r["qty_received"] - r["qty_rejected"]
                lines.append({"invoice_id": inv_id, "line_no": i, "po_line_no": r["line_no"],
                              "qty": qty, "unit_price": price[r["line_no"]],
                              "amount": money(price[r["line_no"]] * qty)})
            term = terms.get(po["contract_id"] or "", 30)
            emit({"invoice_id": inv_id, "po_id": po["po_id"], "supplier_id": po["supplier_id"],
                  "bu_id": bu, "invoice_date": inv_date, "currency": po["currency"]}, lines, term)

        log_sups = [sid for sid, c in self.w.supplier_category.items() if c == "CAT-LOG"]
        n = 0
        for m in month_starts(self.cfg.start, self.cfg.end):
            for _ in range(self.cfg.sizes.pos_per_month // 20 + 1):
                n += 1
                sid = s.choice(log_sups)
                inv_id = f"INV-NP-{n:05d}"
                amount = money(s.decimal("150", "4000", 2)
                               * CURRENCY_FACTOR[self.w.supplier_currency[sid]])
                emit({"invoice_id": inv_id, "po_id": None, "supplier_id": sid,
                      "bu_id": s.choice([b[0] for b in BUSINESS_UNITS]),
                      "invoice_date": m + dt.timedelta(days=s.below(28)),
                      "currency": self.w.supplier_currency[sid]},
                     [{"invoice_id": inv_id, "line_no": 1, "po_line_no": None, "qty": 1,
                       "unit_price": amount, "amount": amount}], 30)
        self.t["invoices"], self.t["invoice_lines"], self.t["payments"] = invoices, ilines, payments

    def budgets(self) -> None:
        s = self.s("budgets")
        cur = {b[0]: b[3] for b in BUSINESS_UNITS}
        rows = []
        for cc in self.t["cost_centers"]:
            for m in month_starts(self.cfg.start, self.cfg.end):
                if m.month % 3 != 1:
                    continue
                quarter = f"{m.year}Q{(m.month - 1) // 3 + 1}"
                factor = CURRENCY_FACTOR[cur[cc["bu_id"]]]
                amount = distinctive(lambda: money(
                    Decimal(s.randint(4_000_000, 40_000_000)) / 100 * factor))  # noqa: B023
                rows.append({"cost_center_id": cc["cc_id"], "quarter": quarter,
                             "amount": amount, "currency": cur[cc["bu_id"]],
                             "row_tag": self.row_tag("budgets", {"cost_center_id": cc["cc_id"],
                                                                 "quarter": quarter})})
        self.t["budgets"] = rows

    def claims_exceptions_risk(self) -> None:
        s = self.s("claims")
        claims = []
        n = 0
        for c in self.w.contracts.values():
            if s.chance(1, 6):
                n += 1
                quarter = s.choice(("2024Q2", "2024Q3", "2024Q4", "2025Q1"))
                claims.append({
                    "claim_id": f"SCC-{n:04d}", "contract_id": c.contract_id, "quarter": quarter,
                    "amount": distinctive(lambda: money(
                        s.decimal("1200", "9000", 2)
                        * CURRENCY_FACTOR[self.w.supplier_currency[c.supplier_id]])),  # noqa: B023
                    "currency": self.w.supplier_currency[c.supplier_id],
                    "status": s.choice(("paid", "disputed", "open")),
                    "row_tag": self.row_tag("service_credit_claims", {"claim_id": f"SCC-{n:04d}"}),
                })
        self.t["service_credit_claims"] = claims
        self.t["policy_exceptions"] = [
            {k: e[k] for k in ("exception_id", "po_id", "reason", "approver_role", "amount",
                               "currency", "approved_on")}
            | {"doc_id": f"DOC-EXC-{e['exception_id']}",
               "row_tag": self.row_tag("policy_exceptions", {"exception_id": e["exception_id"]})}
            for e in self.w.exceptions
        ]
        sr = self.s("risk")
        risk = []
        for sup in self.t["suppliers"]:
            for year in (2024, 2025):
                assessed = dt.date(year, 3, 1) + dt.timedelta(days=sr.below(60))
                score = sr.decimal("38", "96", 5)
                rating = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"
                pk = {"supplier_id": sup["supplier_id"], "assessed_on": assessed.isoformat()}
                loc = {"kind": "cell", "table": "supplier_risk_ratings", "pk": pk}
                risk.append({
                    "supplier_id": sup["supplier_id"], "assessed_on": assessed, "rating": rating,
                    "score": score,
                    "notes": "Assessor notes ref " + self.mint.mint({**loc, "column": "notes"})
                    + ": financial and delivery review.",
                    "row_tag": self.row_tag("supplier_risk_ratings", pk),
                })
                self.inst.sensitive_values.append({
                    "value": format(score, "f"), "kind": "risk_score",
                    "location": {**loc, "column": "score"}})
        self.t["supplier_risk_ratings"] = risk

    # ------------------------------------------------------------------ orchestration
    def build(self) -> World:
        self.reference()
        self.suppliers()
        self.contracts()
        self.plan_s1()
        self.contract_rows()
        self.random_pos()
        self.receipts()
        self.planted_pos_s1()
        for ln in self.t["po_lines"]:
            if ln["po_id"].startswith("PO-S1-"):
                self._receipt(self.s("scenario/s1/receipts"), ln, -1, None)
        self.planted_s2()
        self.planted_s3()
        self.incidents()
        self.invoices_and_payments()
        self.budgets()
        self.claims_exceptions_risk()
        for name in ("purchase_orders", "po_lines", "goods_receipts"):
            self.t[name].sort(key=_pk_sort_key(name))
        return self.w


def _pk_sort_key(table_name: str) -> Any:
    from eeb.schema import table

    pk = table(table_name).pk
    return lambda r: tuple(str(r[c]) if not isinstance(r[c], int) else f"{r[c]:010d}" for c in pk)
