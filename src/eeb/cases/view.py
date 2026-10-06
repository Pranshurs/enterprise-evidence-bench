"""Read-only view of an instance directory for case building, with per-principal visibility.

Gold values are always computed from these source artifacts: rows from
``tables/*.jsonl``, text from ``docs/*.md``. A principal's view contains exactly the rows,
columns and chunks the policy oracle grants. Nothing here is typed by hand.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from eeb.db.load import _parse, parsed_assignments, read_instance, read_jsonl
from eeb.policy import schema as pschema
from eeb.policy.oracle import Oracle, pk_key
from eeb.schema import TABLES


@dataclass
class InstanceData:
    root: Path
    meta: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    docs: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    oracle: Oracle | None = None
    _visible: dict[tuple[str, str], tuple[dict[str, Any], ...]] = field(
        default_factory=dict, repr=False)
    _by: dict[tuple[str, str, str], dict[Any, list[dict[str, Any]]]] = field(
        default_factory=dict, repr=False)

    @classmethod
    def load(cls, root: Path) -> InstanceData:
        meta = read_instance(root)
        tables = {}
        for t in TABLES:
            recs = read_jsonl(root / f"tables/{t.name}.jsonl")
            tables[t.name] = [dict(zip(t.column_names, _parse(t, r), strict=True)) for r in recs]
        docs = {}
        for m in read_jsonl(root / "manifest/documents.jsonl"):
            m = dict(m)
            m["rendered"] = (root / m["path"]).read_text("utf-8")
            for k in ("effective_from", "effective_to"):
                m[k] = dt.date.fromisoformat(m[k]) if m[k] else None
            docs[(m["doc_id"], m["version"])] = m
        policy = pschema.load_policy((root / "policy.yaml").read_bytes())
        today = dt.date.fromisoformat(meta["config"]["today"])
        oracle = Oracle(policy, tables, parsed_assignments(root), today)
        return cls(root, meta, tables, docs, oracle)

    @property
    def today(self) -> dt.date:
        return dt.date.fromisoformat(self.meta["config"]["today"])

    @cached_property
    def principals(self) -> list[dict[str, Any]]:
        return read_jsonl(self.root / "principals.jsonl")

    @cached_property
    def injections(self) -> list[dict[str, Any]]:
        return read_jsonl(self.root / "registry/injections.jsonl")

    @cached_property
    def conflicts(self) -> list[dict[str, Any]]:
        return read_jsonl(self.root / "registry/conflicts.jsonl")

    @cached_property
    def chunks(self) -> list[dict[str, Any]]:
        return self.tables["doc_chunks"]

    def by(self, table: str, key: str) -> dict[Any, dict[str, Any]]:
        return {r[key]: r for r in self.tables[table]}

    def view(self, principal_id: str) -> PrincipalView:
        return PrincipalView(self, principal_id)

    def global_view(self) -> GlobalView:
        return GlobalView(self)

    def find_span(self, doc_id: str, version: int, phrase: str) -> dict[str, Any] | None:
        doc = self.docs.get((doc_id, version))
        if doc is None:
            return None
        text = doc["rendered"]
        start = text.find(phrase)
        if start < 0 or text.find(phrase, start + 1) >= 0:
            return None
        end = start + len(phrase)
        chunk = self.chunk_for_span(doc_id, version, phrase)
        return {"doc_id": doc_id, "version": version, "start": start, "end": end,
                "phrase": phrase, "chunk_id": chunk}

    def chunk_for_span(self, doc_id: str, version: int, phrase: str) -> str | None:
        hits = [c["chunk_id"] for c in self.chunks if c["doc_id"] == doc_id
                and c["version"] == version and phrase in c["text"]]
        return hits[0] if len(hits) == 1 else None

    def doc_versions(self, doc_id: str) -> list[dict[str, Any]]:
        return sorted((d for (i, _), d in self.docs.items() if i == doc_id),
                      key=lambda d: d["version"])

    def version_effective_on(self, doc_id: str, on: dt.date) -> int | None:
        for d in self.doc_versions(doc_id):
            if d["effective_from"] <= on and (d["effective_to"] is None or on <= d["effective_to"]):
                return int(d["version"])
        return None


class PrincipalView:
    """Rows, columns and chunks visible to one principal (decided by the policy oracle)."""

    def __init__(self, data: InstanceData, principal_id: str) -> None:
        self.data = data
        self.pid = principal_id
        assert data.oracle is not None
        self.oracle = data.oracle

    def can_read(self, table: str, columns: list[str]) -> bool:
        return self.oracle.privileged(self.pid, table) and set(columns) <= set(
            self.oracle.columns(self.pid, table))

    def rows(self, table: str) -> list[dict[str, Any]]:
        """Visible rows in table order. The oracle decides once per (principal, table);
        the instance is read-only, so the decision is reused."""
        key = (self.pid, table)
        if key not in self.data._visible:
            if not self.oracle.privileged(self.pid, table):
                self.data._visible[key] = ()
            else:
                vis = self.oracle.visible_rows(self.pid, table)
                self.data._visible[key] = tuple(
                    r for r in self.data.tables[table] if pk_key(table, r) in vis)
        return list(self.data._visible[key])

    def by(self, table: str, column: str) -> dict[Any, list[dict[str, Any]]]:
        """Visible rows of ``table`` grouped by ``column``, in table order (built once)."""
        key = (self.pid, table, column)
        if key not in self.data._by:
            idx: dict[Any, list[dict[str, Any]]] = {}
            for r in self.rows(table):
                idx.setdefault(r[column], []).append(r)
            self.data._by[key] = idx
        return self.data._by[key]

    def chunk_visible(self, chunk_id: str | None) -> bool:
        return chunk_id is not None and (chunk_id,) in self.oracle.visible_rows(
            self.pid, "doc_chunks") and "text" in self.oracle.columns(self.pid, "doc_chunks")


class GlobalView(PrincipalView):
    """Every row and chunk (used for the unauthorized side of restricted-value probes)."""

    def __init__(self, data: InstanceData) -> None:
        self.data = data
        self.pid = "__global__"

    def can_read(self, table: str, columns: list[str]) -> bool:
        return True

    def rows(self, table: str) -> list[dict[str, Any]]:
        return list(self.data.tables[table])

    def chunk_visible(self, chunk_id: str | None) -> bool:
        return chunk_id is not None


def dumps(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str)
