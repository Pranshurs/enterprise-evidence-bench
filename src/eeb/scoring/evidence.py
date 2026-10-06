"""What the scorers look up about the frozen instance: document text and versions, chunk
boundaries, effective dates and the policy oracle's grants.

Chunk boundaries are not stored with offsets; every chunk's text occurs exactly once in
its rendered document (checked when the context is built), so its span is found once.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from eeb.cases.view import InstanceData


def quarter_bounds(q: str) -> tuple[dt.date, dt.date]:
    year, n = int(q[:4]), int(q[5])
    start = dt.date(year, 3 * n - 2, 1)
    end = (dt.date(year + 1, 1, 1) if n == 4 else dt.date(year, 3 * n + 1, 1)) \
        - dt.timedelta(days=1)
    return start, end


@dataclass
class Evidence:
    data: InstanceData
    _spans: dict[tuple[str, int], list[tuple[int, int, str]]] = field(default_factory=dict)
    _visible: dict[tuple[str, str], bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for c in sorted(self.data.chunks, key=lambda c: (c["doc_id"], c["version"],
                                                          c["chunk_index"])):
            doc = self.data.docs.get((c["doc_id"], c["version"]))
            if doc is None:
                raise ValueError(f"chunk {c['chunk_id']} has no document")
            text = doc["rendered"]
            start = text.find(c["text"])
            if start < 0 or text.find(c["text"], start + 1) >= 0:
                raise ValueError(f"chunk {c['chunk_id']} does not occur exactly once")
            self._spans.setdefault((c["doc_id"], c["version"]), []).append(
                (start, start + len(c["text"]), c["chunk_id"]))

    def text(self, doc_id: str, version: int) -> str | None:
        d = self.data.docs.get((doc_id, version))
        return None if d is None else str(d["rendered"])

    def chunks_over(self, doc_id: str, version: int, start: int, end: int
                    ) -> list[str] | None:
        """The chunks a span overlaps; None if part of the span lies outside every chunk."""
        spans = self._spans.get((doc_id, version), [])
        hit = [(s, e, cid) for s, e, cid in spans if s < end and start < e]
        covered = start
        for s, e, _ in hit:
            if s > covered:
                return None
            covered = max(covered, e)
        return [cid for _, _, cid in hit] if hit and covered >= end else None

    def granted(self, principal_id: str, chunk_id: str) -> bool:
        key = (principal_id, chunk_id)
        if key not in self._visible:
            self._visible[key] = self.data.view(principal_id).chunk_visible(chunk_id)
        return self._visible[key]

    def effective_range(self, doc_id: str, version: int) -> tuple[dt.date, dt.date | None]:
        d = self.data.docs[(doc_id, version)]
        return d["effective_from"], d["effective_to"]

    def effective_during(self, doc_id: str, version: int,
                         period: tuple[dt.date, dt.date]) -> bool:
        """The version was in force at some date of ``period``."""
        lo, hi = self.effective_range(doc_id, version)
        return lo <= period[1] and (hi is None or period[0] <= hi)


def case_period(case: dict[str, Any]) -> tuple[dt.date, dt.date]:
    """The period a case asks about: its temporal period or date, else its quarter slot,
    else the as-of date."""
    temporal = case.get("temporal") or {}
    if temporal.get("period"):
        return quarter_bounds(temporal["period"])
    if temporal.get("as_of"):
        day = dt.date.fromisoformat(str(temporal["as_of"]))
        return day, day
    slots = case.get("slots") or {}
    if "quarter" in slots:
        return quarter_bounds(slots["quarter"])
    if "quarter_before" in slots and "quarter_after" in slots:
        return quarter_bounds(slots["quarter_before"])[0], \
            quarter_bounds(slots["quarter_after"])[1]
    day = dt.date.fromisoformat(case["as_of"])
    return day, day
