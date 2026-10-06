"""Mechanical case validation (spec §6.4; validation requirements for the case/gold milestone).

Each validator returns a list of problems; a candidate binding with any problem is
rejected, and the rejection reason is counted in the build report.

- ``authorization``: an ANSWER principal can read every evidence unit; a denied family
  member misses at least one *necessary* unit.
- ``necessity``: a cross-source (X) case is proven to need both sources. A resolver
  limited to SQL fails on at least one required fact, and so does a resolver limited to
  documents.
- ``metric_layer``: whether any permitted metric query (filters drawn from the case's own
  slots, grouping on up to two declared dimensions) reconstructs every SQL leaf fact. An
  out-of-layer case must not be reconstructible; an in-layer case must be.
- ``restricted_probe``: where the principal's answer differs from the all-rows answer,
  both values are recorded.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from decimal import Decimal
from typing import Any

from eeb.cases.view import InstanceData, PrincipalView
from eeb.metrics import layer, reference

SLOT_FILTERS = {"supplier_id": ["supplier_id"], "currency": ["currency"],
                "item_id": ["item_id"], "po_id": ["po_id"],
                "quarter": ["invoice_quarter", "order_quarter", "promised_quarter", "quarter"]}
_NUM = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d])")


def _num(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, Decimal)):
        return Decimal(v)
    return None


# ---------------------------------------------------------------------------- authorization
def missing_evidence(gold: dict[str, Any], view: PrincipalView) -> list[str]:
    missing: list[str] = []
    for f in gold["facts"]:
        if f["source"] == "sql":
            for t, cols in f["uses"].items():
                if not view.can_read(t, cols):
                    missing.append(f"{f['fact_id']}: cannot read {t}({','.join(cols)})")
        elif f["source"] == "doc" and not view.chunk_visible(f["doc_ref"]["chunk_id"]):
            missing.append(f"{f['fact_id']}: chunk {f['doc_ref']['chunk_id']} not visible")
    return missing


# ---------------------------------------------------------------------------- necessity
def _closure(gold: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {f["fact_id"]: f for f in gold["facts"]}
    out, stack = [], list(gold["answer_requirement"])
    seen: set[str] = set()
    while stack:
        fid = stack.pop()
        if fid in seen:
            continue
        seen.add(fid)
        f = by_id[fid]
        out.append(f)
        if f["source"] == "derived":
            stack.extend(f["derived"]["inputs"])
    return out


def _entity_rows(data: InstanceData, slots: dict[str, Any], f: dict[str, Any]
                 ) -> list[dict[str, Any]]:
    rows = []
    cid = slots.get("contract_id")
    if cid is None and f.get("doc_ref"):
        m = re.search(r"(CTR-\d+)", f["doc_ref"]["doc_id"])
        cid = m.group(1) if m else None
    if cid:
        rows += [c for c in data.tables["contracts"] if c["contract_id"] == cid]
        rows += [p for p in data.tables["contract_price_schedule"] if p["contract_id"] == cid]
    if "supplier_id" in slots:
        rows += [s for s in data.tables["suppliers"] if s["supplier_id"] == slots["supplier_id"]]
    return rows


def sql_resolvable(data: InstanceData, slots: dict[str, Any], f: dict[str, Any],
                   by_id: dict[str, dict[str, Any]]) -> bool:
    if f["source"] == "sql":
        return True
    if f["source"] == "derived":
        return all(sql_resolvable(data, slots, by_id[i], by_id) for i in f["derived"]["inputs"])
    want = _num(f["value"])
    for row in _entity_rows(data, slots, f):
        for v in row.values():
            n = _num(v)
            if want is not None and n is not None and n == want:
                return True
    return False


def doc_resolvable(data: InstanceData, slots: dict[str, Any], f: dict[str, Any],
                   by_id: dict[str, dict[str, Any]]) -> bool:
    if f["source"] == "doc":
        return True
    if f["source"] == "derived":
        return all(doc_resolvable(data, slots, by_id[i], by_id) for i in f["derived"]["inputs"])
    keys = [str(slots[k]) for k in ("supplier_id", "contract_id") if k in slots]
    texts = [d["rendered"] for d in data.docs.values() if any(k in d["rendered"] for k in keys)]
    if f["kind"] == "entity_set":
        ids = f["value"]
        return bool(ids) and all(any(i in d["rendered"] for d in data.docs.values()) for i in ids)
    want = _num(f["value"])
    if want is None:
        return False
    for t in texts:
        for m in _NUM.finditer(t):
            if Decimal(m.group(0).replace(",", "")) == want:
                return True
    return False


def necessity(data: InstanceData, slots: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    by_id = {f["fact_id"]: f for f in gold["facts"]}
    leaves = [f for f in _closure(gold) if f["source"] != "derived"]
    sql_missing = [f["fact_id"] for f in leaves if not sql_resolvable(data, slots, f, by_id)]
    doc_missing = [f["fact_id"] for f in leaves if not doc_resolvable(data, slots, f, by_id)]
    return {"sql_alone_sufficient": not sql_missing, "docs_alone_sufficient": not doc_missing,
            "sql_alone_lacks": sorted(sql_missing), "docs_alone_lack": sorted(doc_missing)}


# ---------------------------------------------------------------------------- metric layer
class MetricSearch:
    """Searches the governed catalog for a query that reproduces a value.

    The search space is every catalog metric, every subset of the filters the case's own
    slots allow, and every grouping on up to two declared dimensions. The numeric results
    of one (principal, metric, filter subset, grouping) query do not depend on the value
    being searched for, so they are computed once and reused; filtered row sets come from
    a per-dimension index rather than a scan. Neither changes which queries are tried.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], list[dict[str, Any]] | None] = {}
        self._index: dict[tuple[str, str, str], dict[str, list[int]]] = {}
        self._subsets: dict[tuple[str, str, tuple[tuple[str, str], ...]],
                            list[dict[str, Any]]] = {}
        self._values: dict[tuple[Any, ...], tuple[Decimal, ...]] = {}

    def _view(self, view: PrincipalView, name: str) -> list[dict[str, Any]] | None:
        key = (view.pid, name)
        if key not in self._rows:
            self._rows[key] = reference.view_rows(view, name)
        return self._rows[key]

    def _positions(self, pid: str, name: str, rows: list[dict[str, Any]],
                   dim: str) -> dict[str, list[int]]:
        key = (pid, name, dim)
        if key not in self._index:
            idx: dict[str, list[int]] = {}
            for i, r in enumerate(rows):
                idx.setdefault(str(r[dim]), []).append(i)
            self._index[key] = idx
        return self._index[key]

    def _subset(self, pid: str, name: str, rows: list[dict[str, Any]],
                fsub: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
        """Rows matching every filter in ``fsub``, in view order."""
        if not fsub:
            return rows
        key = (pid, name, fsub)
        if key not in self._subsets:
            (d0, v0), rest = fsub[0], fsub[1:]
            self._subsets[key] = [
                r for r in (rows[i] for i in self._positions(pid, name, rows, d0).get(v0, []))
                if all(str(r[d]) == val for d, val in rest)]
        return self._subsets[key]

    def _query_values(self, pid: str, metric: str, m: dict[str, Any],
                      rows: list[dict[str, Any]], fsub: tuple[tuple[str, str], ...],
                      gb: tuple[str, ...]) -> tuple[Decimal, ...]:
        """Every numeric result of one metric query (one value per group)."""
        key = (pid, metric, fsub, gb)
        if key not in self._values:
            sub = self._subset(pid, m["view"], rows, fsub)
            group = list(dict.fromkeys(list(m["always_group_by"]) + list(gb)))
            buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for r in sub:
                buckets.setdefault(tuple(str(r[x]) for x in group), []).append(r)
            if not group:
                buckets = {(): sub}
            out = []
            for members in buckets.values():
                if not members and metric != "po_count":
                    continue
                n = _num(reference.measure(metric, members))
                if n is not None:
                    out.append(n)
            self._values[key] = tuple(out)
        return self._values[key]

    def reconstructible(self, view: PrincipalView, slots: dict[str, Any], value: Any,
                        tolerance: str) -> bool:
        want = _num(value)
        if want is None:
            return False
        tol = Decimal(tolerance)
        for metric, m in layer.catalog()["metrics"].items():
            rows = self._view(view, m["view"])
            if rows is None:
                continue
            applicable = [(d, str(slots[s])) for s, dims in SLOT_FILTERS.items() if s in slots
                          for d in dims if d in m["filters"]]
            for k in range(len(applicable) + 1):
                for fsub in itertools.combinations(applicable, k):
                    for g in range(0, 3):
                        for gb in itertools.combinations(m["dimensions"], g):
                            if any(abs(n - want) <= tol for n in
                                   self._query_values(view.pid, metric, m, rows, fsub, gb)):
                                return True
        return False


def metric_layer_membership(search: MetricSearch, view: PrincipalView, slots: dict[str, Any],
                            gold: dict[str, Any]) -> dict[str, Any]:
    leaves = [f for f in _closure(gold) if f["source"] == "sql"]
    if not leaves:
        return {"in_metric_layer": None, "unreconstructible": []}
    missing = [f["fact_id"] for f in leaves
               if not search.reconstructible(view, slots, f["value"], f["tolerance"])]
    return {"in_metric_layer": not missing, "unreconstructible": missing}


def restricted_probe(gold: dict[str, Any], global_gold: dict[str, Any]) -> list[dict[str, Any]]:
    g = {f["fact_id"]: f for f in global_gold["facts"]}
    out = []
    for f in gold["facts"]:
        if f["source"] == "sql" and f["fact_id"] in g and g[f["fact_id"]]["value"] != f["value"]:
            out.append({"fact_id": f["fact_id"], "authorized_value": f["value"],
                        "unauthorized_value": g[f["fact_id"]]["value"]})
    return out


def as_jsonable(x: Any) -> Any:
    if isinstance(x, Decimal):
        return format(x, "f")
    if isinstance(x, dt.date):
        return x.isoformat()
    if isinstance(x, dict):
        return {k: as_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [as_jsonable(v) for v in x]
    return x
