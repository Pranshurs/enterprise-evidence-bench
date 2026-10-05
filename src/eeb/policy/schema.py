"""Shape validation for ``policy.yaml``. Validation only; no evaluation semantics.

Both the SQL compiler and the Python oracle receive the *raw* mapping returned by
``load_policy``. This module only rejects malformed or unsafe policies, so that a bug here
cannot make the two implementations agree on a wrong answer: it never decides
visibility.
"""

from __future__ import annotations

import re
from importlib import resources
from typing import Any

import yaml

from eeb.schema import BY_NAME

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
_VALUE = re.compile(r"^[A-Za-z0-9_\-]+$")
_OPS = {"eq_param", "in_values", "is_null", "in_parent", "any_of", "all_of"}


class PolicyError(ValueError):
    pass


def policy_bytes() -> bytes:
    return resources.files("eeb").joinpath("data/policy.yaml").read_bytes()


def load_policy(raw: bytes | None = None) -> dict[str, Any]:
    data = yaml.safe_load(raw if raw is not None else policy_bytes())
    validate(data)
    return data  # type: ignore[no-any-return]


def _fail(msg: str) -> None:
    raise PolicyError(msg)


def _granted_columns(entry: dict[str, Any], table: str) -> set[str]:
    cols = entry["columns"]
    return set(BY_NAME[table].column_names) if cols == "*" else set(cols)


def _check_rule(rule: Any, table: str, role: str, param: str | None,
                tables: dict[str, Any], depth: int = 0) -> None:
    if depth > 8:
        _fail(f"{role}.{table}: rule nesting too deep")
    if rule == "all":
        return
    if not isinstance(rule, dict) or len(rule) != 1:
        _fail(f"{role}.{table}: a rule is 'all' or a single-key mapping, got {rule!r}")
    (op, arg), = rule.items()
    if op not in _OPS:
        _fail(f"{role}.{table}: unknown operator {op!r}")
    cols = BY_NAME[table].column_names
    if op == "eq_param":
        if set(arg) != {"column", "param"} or arg["column"] not in cols:
            _fail(f"{role}.{table}: bad eq_param {arg!r}")
        if param is None or arg["param"] != param:
            _fail(f"{role}.{table}: eq_param uses {arg['param']!r} but role param is {param!r}")
    elif op == "in_values":
        if set(arg) != {"column", "values"} or arg["column"] not in cols:
            _fail(f"{role}.{table}: bad in_values {arg!r}")
        if not arg["values"] or not all(isinstance(v, str) and _VALUE.match(v)
                                        for v in arg["values"]):
            _fail(f"{role}.{table}: in_values needs non-empty safe string values")
    elif op == "is_null":
        if arg not in cols:
            _fail(f"{role}.{table}: bad is_null column {arg!r}")
    elif op == "in_parent":
        if set(arg) != {"table", "fk", "pk"} or arg["table"] not in BY_NAME:
            _fail(f"{role}.{table}: bad in_parent {arg!r}")
        parent = BY_NAME[arg["table"]]
        if tuple(arg["pk"]) != parent.pk:
            _fail(f"{role}.{table}: in_parent pk must be the parent primary key {parent.pk}")
        if len(arg["fk"]) != len(arg["pk"]) or any(c not in cols for c in arg["fk"]):
            _fail(f"{role}.{table}: in_parent fk columns invalid")
        if arg["table"] not in tables:
            _fail(f"{role}.{table}: in_parent parent {arg['table']} is not granted to the role")
        if not set(parent.pk) <= _granted_columns(tables[arg["table"]], arg["table"]):
            _fail(f"{role}.{table}: parent primary key columns must be granted to the role")
    else:
        if not isinstance(arg, list) or not arg:
            _fail(f"{role}.{table}: {op} needs a non-empty list")
        for sub in arg:
            _check_rule(sub, table, role, param, tables, depth + 1)


def _parents(rule: Any) -> list[str]:
    if not isinstance(rule, dict):
        return []
    (op, arg), = rule.items()
    if op == "in_parent":
        return [arg["table"]]
    if op in ("any_of", "all_of"):
        return [p for sub in arg for p in _parents(sub)]
    return []


def validate(data: Any) -> None:
    if not isinstance(data, dict) or set(data) != {"policy_version", "roles"}:
        _fail("top level must have exactly policy_version and roles")
    if not isinstance(data["policy_version"], int):
        _fail("policy_version must be an integer")
    roles = data["roles"]
    if not isinstance(roles, dict) or not roles:
        _fail("roles must be a non-empty mapping")
    for role, spec in roles.items():
        if not _IDENT.match(role):
            _fail(f"bad role name {role!r}")
        if not isinstance(spec, dict) or set(spec) != {"param", "tables"}:
            _fail(f"{role}: must have exactly param and tables")
        param = spec["param"]
        if param is not None and not _IDENT.match(param):
            _fail(f"{role}: bad param name")
        tables = spec["tables"]
        if not isinstance(tables, dict):
            _fail(f"{role}: tables must be a mapping")
        for tname, entry in tables.items():
            if tname not in BY_NAME:
                _fail(f"{role}: unknown table {tname!r}")
            if not isinstance(entry, dict) or set(entry) != {"rows", "columns"}:
                _fail(f"{role}.{tname}: needs exactly rows and columns")
            cols = entry["columns"]
            if cols != "*":
                if not isinstance(cols, list) or not cols or len(set(cols)) != len(cols):
                    _fail(f"{role}.{tname}: columns must be '*' or a non-empty unique list")
                unknown = set(cols) - set(BY_NAME[tname].column_names)
                if unknown:
                    _fail(f"{role}.{tname}: unknown columns {sorted(unknown)}")
            if not set(BY_NAME[tname].pk) <= _granted_columns(entry, tname):
                _fail(f"{role}.{tname}: primary key columns must be granted")
            _check_rule(entry["rows"], tname, role, param, tables)
        _check_acyclic(role, tables)


def _check_acyclic(role: str, tables: dict[str, Any]) -> None:
    done: set[str] = set()

    def visit(t: str, stack: tuple[str, ...]) -> None:
        if t in stack:
            _fail(f"{role}: in_parent cycle through {t}")
        if t in done:
            return
        for p in _parents(tables[t]["rows"]):
            visit(p, stack + (t,))
        done.add(t)

    for t in tables:
        visit(t, ())


def validate_principals(principals: list[dict[str, Any]], assignments: list[dict[str, Any]],
                        policy: dict[str, Any]) -> None:
    roles = policy["roles"]
    by_pid: dict[str, set[str]] = {}
    for a in assignments:
        if a["role"] not in roles:
            _fail(f"assignment {a['assignment_id']}: unknown role {a['role']!r}")
        param = roles[a["role"]]["param"]
        if (param is None) != (a["param_value"] is None) or (
                param is not None and a["param_name"] != param):
            _fail(f"assignment {a['assignment_id']}: parameter does not match role")
        if a["param_value"] is not None and not _VALUE.match(a["param_value"]):
            _fail(f"assignment {a['assignment_id']}: unsafe parameter value")
        if a["valid_to"] is not None and a["valid_to"] < a["valid_from"]:
            _fail(f"assignment {a['assignment_id']}: valid_to before valid_from")
        by_pid.setdefault(a["principal_id"], set()).add(a["role"])
    ids = [p["principal_id"] for p in principals]
    if len(ids) != len(set(ids)) or not all(_IDENT.match(i) for i in ids):
        _fail("principal ids must be unique identifiers")
    for pid, rs in by_pid.items():
        if pid not in ids:
            _fail(f"assignment for unknown principal {pid}")
        if len(rs) > 1:
            _fail(f"principal {pid} holds more than one role (ADR-0002)")
