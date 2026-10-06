"""Gold fact construction from source artifacts (spec §6.2).

- **Doc facts** are extracted from rendered document text with a pattern that must match
  exactly once. The span is the exact character range of the match.
- **SQL facts** are computed in Python from the *principal's visible rows* and carry the
  ``gold_sql`` that validation re-executes under that principal's login.
- **Derived facts** declare their operator and inputs.

Nothing here reads generator internals or typed values.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from eeb.cases.view import InstanceData


class FactError(ValueError):
    pass


def rnd(x: Decimal, places: int) -> Decimal:
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def doc_fact(data: InstanceData, fid: str, doc_id: str, version: int, pattern: str,
             kind: str, unit: str | None = None, cast: Any = Decimal) -> dict[str, Any]:
    doc = data.docs.get((doc_id, version))
    if doc is None:
        raise FactError(f"{fid}: no document {doc_id}@v{version}")
    matches = list(re.finditer(pattern, doc["rendered"]))
    if len(matches) != 1:
        raise FactError(f"{fid}: pattern matched {len(matches)} times in {doc_id}@v{version}")
    m = matches[0]
    phrase = m.group(0)
    chunk = data.chunk_for_span(doc_id, version, phrase)
    if chunk is None:
        raise FactError(f"{fid}: span not inside exactly one chunk")
    raw = m.group(1)
    value = cast(raw.replace(",", "")) if cast is Decimal else cast(raw)
    return {"fact_id": fid, "source": "doc", "kind": kind, "value": value, "unit": unit,
            "tolerance": "0",
            "doc_ref": {"doc_id": doc_id, "version": version, "start": m.start(),
                        "end": m.end(), "phrase": phrase, "chunk_id": chunk}}


def sql_fact(fid: str, kind: str, value: Any, gold_sql: str, uses: dict[str, list[str]],
             unit: str | None = None, tolerance: str = "0") -> dict[str, Any]:
    return {"fact_id": fid, "source": "sql", "kind": kind, "value": value, "unit": unit,
            "tolerance": tolerance, "gold_sql": gold_sql, "uses": uses}


def derived_fact(fid: str, kind: str, value: Any, op: str, inputs: list[str],
                 unit: str | None = None, tolerance: str = "0") -> dict[str, Any]:
    return {"fact_id": fid, "source": "derived", "kind": kind, "value": value, "unit": unit,
            "tolerance": tolerance, "derived": {"op": op, "inputs": inputs}}


def lit(v: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", v):
        raise FactError(f"unsafe literal {v!r}")
    return f"'{v}'"


# Patterns for facts stated in documents (the generator's wording is the only source).
P_OTD_TARGET = r"The OTD target is ([\d.]+)%\."
P_CREDIT_RATE = r"a service credit of ([\d.]+)% of the credit base"
P_CAP = r"capped at ([\d.]+)% of the credit base"
P_QUALITY = r"exceed ([\d.]+)% of the quantity received"
P_SURCHARGE = r"carry a surcharge of ([\d.]+)% on the scheduled unit price"
P_PAYTERMS = r"Invoices are payable within (\d+) days of the invoice date"
P_INDEXATION = r"are increased by ([\d.]+)%"
P_INDEXATION_DATE = r"with effect from (\d{1,2} [A-Z][a-z]+ \d{4})"
P_FORCE_MAJEURE = r"Classified as force majeure: (yes|no)\."


def p_threshold(currency: str) -> str:
    return rf"\b{currency} ([\d,]+)\b"
