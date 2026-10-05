"""Policy → pure-Python authorization oracle (implementation 2 of 2; spec §5.2).

Evaluates the raw policy mapping directly against instance rows. Shares no code with
``sqlgen.py``; the two meet only in the canonical policy file and in the agreement check
(``eeb.db.agreement``).

Outcome model per (principal, table):
- ``privileged``: the principal may issue SELECT on the table at all;
- ``columns``: sorted granted columns;
- ``rows``: sorted primary keys of visible rows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from eeb.schema import BY_NAME, TABLES


def pk_key(table: str, row: Mapping[str, Any]) -> tuple[str, ...]:
    """Canonical primary-key tuple; every element rendered as text."""
    out = []
    for c in BY_NAME[table].pk:
        v = row[c]
        out.append(v.isoformat() if isinstance(v, dt.date) else str(v))
    return tuple(out)


class Oracle:
    def __init__(self, policy: Mapping[str, Any], tables: Mapping[str, Sequence[Mapping[str, Any]]],
                 assignments: Sequence[Mapping[str, Any]], today: dt.date) -> None:
        self._roles = policy["roles"]
        self._tables = tables
        self._today = today
        self._assignments = assignments
        self._memo: dict[tuple[str, str], frozenset[tuple[str, ...]]] = {}

    def active(self, principal_id: str) -> list[Mapping[str, Any]]:
        out = []
        for a in self._assignments:
            if a["principal_id"] != principal_id:
                continue
            started = a["valid_from"] <= self._today
            not_ended = a["valid_to"] is None or self._today <= a["valid_to"]
            if started and not_ended:
                out.append(a)
        return out

    def _entries(self, principal_id: str, table: str) -> list[tuple[Mapping[str, Any], Any]]:
        res = []
        for a in self.active(principal_id):
            spec = self._roles.get(a["role"])
            if spec is not None and table in spec["tables"]:
                res.append((a, spec["tables"][table]))
        return res

    def privileged(self, principal_id: str, table: str) -> bool:
        return bool(self._entries(principal_id, table))

    def columns(self, principal_id: str, table: str) -> list[str]:
        granted: set[str] = set()
        for _, entry in self._entries(principal_id, table):
            if entry["columns"] == "*":
                granted.update(BY_NAME[table].column_names)
            else:
                granted.update(entry["columns"])
        return sorted(granted)

    def _holds(self, rule: Any, row: Mapping[str, Any], param_value: Any,
               principal_id: str) -> bool:
        if rule == "all":
            return True
        if not isinstance(rule, Mapping) or len(rule) != 1:
            raise ValueError(f"malformed rule {rule!r}")
        op = next(iter(rule))
        arg = rule[op]
        if op == "eq_param":
            v = row[arg["column"]]
            return v is not None and param_value is not None and v == param_value
        if op == "in_values":
            v = row[arg["column"]]
            return v is not None and v in arg["values"]
        if op == "is_null":
            return row[arg] is None
        if op == "in_parent":
            fk = [row[c] for c in arg["fk"]]
            if any(v is None for v in fk):
                return False
            key = tuple(v.isoformat() if isinstance(v, dt.date) else str(v) for v in fk)
            return key in self.visible_rows(principal_id, arg["table"])
        if op == "any_of":
            return any(self._holds(r, row, param_value, principal_id) for r in arg)
        if op == "all_of":
            return all(self._holds(r, row, param_value, principal_id) for r in arg)
        raise ValueError(f"unknown operator {op!r}")

    def visible_rows(self, principal_id: str, table: str) -> frozenset[tuple[str, ...]]:
        key = (principal_id, table)
        if key not in self._memo:
            entries = self._entries(principal_id, table)
            visible = set()
            if entries:
                for row in self._tables[table]:
                    for a, entry in entries:
                        if self._holds(entry["rows"], row, a["param_value"], principal_id):
                            visible.add(pk_key(table, row))
                            break
            self._memo[key] = frozenset(visible)
        return self._memo[key]

    def may_see_row(self, principal_id: str, table: str, row: Mapping[str, Any]) -> bool:
        return pk_key(table, row) in self.visible_rows(principal_id, table)

    def may_see_cell(self, principal_id: str, table: str, row: Mapping[str, Any],
                     column: str) -> bool:
        return self.may_see_row(principal_id, table, row) and column in self.columns(
            principal_id, table)

    def outcome(self, principal_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for pid in principal_ids:
            per: dict[str, Any] = {}
            for t in TABLES:
                priv = self.privileged(pid, t.name)
                per[t.name] = {
                    "privileged": priv,
                    "columns": self.columns(pid, t.name) if priv else [],
                    "rows": sorted(list(k) for k in self.visible_rows(pid, t.name)) if priv else [],
                }
            out[pid] = per
        return out
