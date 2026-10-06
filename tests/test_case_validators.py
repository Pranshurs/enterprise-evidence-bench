"""Case validators: the metric-layer search must equal an exhaustive, uncached search.

``MetricSearch`` indexes rows and reuses query results so the default-scale corpus can be
built. That is only acceptable if it answers exactly what the plain search answers. The
plain search is kept here, scanning the full view for every query, and both are compared
query by query and verdict by verdict.
"""

from __future__ import annotations

import itertools
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eeb.cases.templates import Ctx
from eeb.cases.validate import SLOT_FILTERS, MetricSearch, _num
from eeb.cases.view import InstanceData, PrincipalView
from eeb.metrics import layer, reference

PRINCIPALS = ["cm_met", "cm_elc", "cm_multi", "cm_moved", "buyer_in", "buyer_eu", "ap_in",
              "ap_eu", "ap_uk", "fin_ctrl", "legal", "risk"]


# ------------------------------------------------------------------ exhaustive reference
def exhaustive_values(view: PrincipalView, slots: dict[str, Any],
                      slot_filters: dict[str, list[str]] | None = None
                      ) -> Counter[tuple[Any, ...]]:
    """Every (metric, filters, grouping, value) the search may compare against, computed
    by scanning the whole view for each query. ``slot_filters`` replaces the slot-to-filter
    mapping when a test states it independently."""
    out: Counter[tuple[Any, ...]] = Counter()
    mapping = SLOT_FILTERS if slot_filters is None else slot_filters
    for metric, m in layer.catalog()["metrics"].items():
        rows = reference.view_rows(view, m["view"])
        if rows is None:
            continue
        applicable = [(d, str(slots[s])) for s, dims in mapping.items() if s in slots
                      for d in dims if d in m["filters"]]
        for k in range(len(applicable) + 1):
            for fsub in itertools.combinations(applicable, k):
                sub = [r for r in rows if all(str(r[d]) == val for d, val in fsub)]
                for g in range(0, 3):
                    for gb in itertools.combinations(m["dimensions"], g):
                        group = list(dict.fromkeys(list(m["always_group_by"]) + list(gb)))
                        buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
                        for r in sub:
                            buckets.setdefault(tuple(str(r[x]) for x in group), []).append(r)
                        if not group:
                            buckets = {(): sub}
                        for members in buckets.values():
                            if not members and metric != "po_count":
                                continue
                            n = _num(reference.measure(metric, members))
                            if n is not None:
                                out[(metric, fsub, gb, n)] += 1
    return out


def exhaustive_reconstructible(values: Counter[tuple[Any, ...]], value: Any,
                               tolerance: str) -> bool:
    want = _num(value)
    if want is None:
        return False
    tol = Decimal(tolerance)
    return any(abs(key[3] - want) <= tol for key in values)


def searched_values(search: MetricSearch, view: PrincipalView,
                    slots: dict[str, Any]) -> Counter[tuple[Any, ...]]:
    out: Counter[tuple[Any, ...]] = Counter()
    for metric, m in layer.catalog()["metrics"].items():
        rows = search._view(view, m["view"])
        if rows is None:
            continue
        applicable = [(d, str(slots[s])) for s, dims in SLOT_FILTERS.items() if s in slots
                      for d in dims if d in m["filters"]]
        for k in range(len(applicable) + 1):
            for fsub in itertools.combinations(applicable, k):
                for g in range(0, 3):
                    for gb in itertools.combinations(m["dimensions"], g):
                        for n in search._query_values(view.pid, metric, m, rows, fsub, gb):
                            out[(metric, fsub, gb, n)] += 1
    return out


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def data(instance_dir: Path) -> InstanceData:
    return InstanceData.load(instance_dir)


def _slot_sets(data: InstanceData) -> list[dict[str, Any]]:
    ctx = Ctx.build(data)
    sups = ctx.active_suppliers()
    pos = sorted(data.tables["purchase_orders"], key=lambda p: p["po_id"])
    lines = sorted(data.tables["po_lines"], key=lambda r: (r["po_id"], r["line_no"]))
    out: list[dict[str, Any]] = [{}]
    for i, sid in enumerate(sups[:6]):
        q = ctx.quarters[i % len(ctx.quarters)]
        out.append({"supplier_id": sid})
        out.append({"supplier_id": sid, "quarter": q})
    for i in (0, len(pos) // 2, len(pos) - 1):
        p = pos[i]
        out.append({"supplier_id": p["supplier_id"], "currency": p["currency"],
                    "po_id": p["po_id"], "quarter": reference.quarter(p["order_date"])})
    for i in (0, len(lines) // 3):
        out.append({"item_id": lines[i]["item_id"], "quarter": ctx.quarters[-1]})
    # A supplier and a quarter that match no row: empty filtered sets.
    out.append({"supplier_id": "SUP-NONE", "quarter": "1999Q1"})
    return out


# ------------------------------------------------------------------ tests
def test_search_enumerates_exactly_the_exhaustive_queries(data: InstanceData) -> None:
    search = MetricSearch()
    compared = 0
    for pid in PRINCIPALS:
        view = data.view(pid)
        for slots in _slot_sets(data):
            want = exhaustive_values(view, slots)
            assert searched_values(search, view, slots) == want, (pid, slots)
            compared += sum(want.values())
    assert compared > 10_000  # the comparison is not vacuous


def test_search_verdict_equals_exhaustive_verdict(data: InstanceData) -> None:
    search = MetricSearch()
    verdicts: Counter[bool] = Counter()
    for pid in PRINCIPALS:
        view = data.view(pid)
        for slots in _slot_sets(data):
            exhaustive = exhaustive_values(view, slots)
            values = sorted({k[3] for k in exhaustive})
            probes: list[tuple[Any, str]] = [(Decimal("123456.78"), "0.01"), (None, "0.01"),
                                             (True, "0.01"), ("12", "0.01"), (0, "0.01")]
            for n in values[:: max(1, len(values) // 12)]:
                probes += [(n, "0.01"), (n + Decimal("0.01"), "0.01"),
                           (n + Decimal("0.011"), "0.01"), (n - Decimal("0.0002"), "0.0001")]
            for value, tol in probes:
                got = search.reconstructible(view, slots, value, tol)
                assert got == exhaustive_reconstructible(exhaustive, value, tol), (
                    pid, slots, value, tol)
                verdicts[got] += 1
    assert verdicts[True] > 100 and verdicts[False] > 100


def test_search_results_do_not_depend_on_call_order(data: InstanceData) -> None:
    view = data.view("fin_ctrl")
    sets = _slot_sets(data)
    forward, backward = MetricSearch(), MetricSearch()
    a = [searched_values(forward, view, s) for s in sets]
    b = [searched_values(backward, view, s) for s in reversed(sets)]
    assert a == list(reversed(b))
    assert a == [searched_values(forward, view, s) for s in sets]  # warm cache, same answers


def test_red_arm_off_contract_slot_filters_the_search(data: InstanceData) -> None:
    """An ``off_contract`` slot must narrow the governed queries the search tries. Some
    value is reachable only with that filter plus two groupings (grouping by
    ``off_contract`` instead would need a third dimension); the search must find it.
    The slot-to-filter mapping is written out here, not taken from the code under test."""
    with_filter = {"supplier_id": ["supplier_id"], "quarter": ["order_quarter"],
                   "off_contract": ["off_contract"]}
    without = {k: v for k, v in with_filter.items() if k != "off_contract"}
    ctx = Ctx.build(data)
    view = data.view("fin_ctrl")
    for sid in ctx.active_suppliers():
        for q in ctx.quarters:
            slots = {"supplier_id": sid, "quarter": q, "off_contract": "yes"}
            reach = {k[3] for k in exhaustive_values(view, slots, with_filter)}
            base = sorted({k[3] for k in exhaustive_values(view, slots, without)})
            only = sorted(v for v in reach
                          if not any(abs(v - b) <= Decimal("0.01") for b in base))
            if only:
                assert MetricSearch().reconstructible(view, slots, only[0], "0.01")
                return
    raise AssertionError("no value is reachable only through the off_contract filter")
