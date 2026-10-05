"""Falsifiability of the agreement check: planted bugs in EITHER implementation must surface
as disagreements (not merely as hardening findings).

Equivalent mutants (no observable change on this policy) are listed at the end with the
reason they cannot be killed, so the accounting is explicit.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from eeb.db import agreement
from eeb.db.load import build_database, drop
from eeb.policy import sqlgen
from eeb.policy.oracle import Oracle
from tests.conftest import unique_ns

pytestmark = pytest.mark.pg


def _wrap_pred(op: str, replacement: str) -> Callable[..., str]:
    orig = sqlgen._pred

    def mutant(rule: Any, table: str, depth: int = 0) -> str:
        if isinstance(rule, dict) and op in rule:
            return replacement
        return orig(rule, table, depth)

    return mutant


def _filter_security(pred: Callable[[str], bool],
                     rewrite: Callable[[str], str] = lambda s: s) -> Callable[..., list[str]]:
    orig = sqlgen.security_sql

    def mutant(*a: Any, **k: Any) -> list[str]:
        return [rewrite(s) for s in orig(*a, **k) if pred(s)]

    return mutant


def _widen_column_grants(stmt: str) -> str:
    if stmt.startswith("GRANT SELECT (") and "supplier_contacts" in stmt:
        target = stmt[stmt.index(" ON ") + 4: stmt.index(" TO ")]
        return f"GRANT SELECT ON {target} TO {stmt[stmt.index(' TO ') + 4:]}"
    return stmt


SQL_MUTANTS: dict[str, tuple[str, Any]] = {
    "active_ignores_valid_to": ("_active_clause", lambda alias, role: (
        f"{alias}.login = current_user AND {alias}.role = {sqlgen.ql(role)} "
        f"AND {alias}.valid_from <= c.today")),
    "eq_param_true": ("_pred", _wrap_pred("eq_param", "TRUE")),
    "in_parent_true": ("_pred", _wrap_pred("in_parent", "TRUE")),
    "in_values_true": ("_pred", _wrap_pred("in_values", "TRUE")),
    "membership_ignores_validity": ("_is_active", lambda a, today: True),
    "column_grant_widened": ("security_sql", _filter_security(lambda s: True,
                                                              _widen_column_grants)),
}


@pytest.mark.parametrize("name", sorted(SQL_MUTANTS))
def test_sql_mutant_is_detected(name: str, pg_dsn: str, instance_dir: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    attr, replacement = SQL_MUTANTS[name]
    monkeypatch.setattr(sqlgen, attr, replacement)
    ns = unique_ns("mut")
    build_database(pg_dsn, instance_dir, ns)
    try:
        result = agreement.check(pg_dsn, instance_dir, ns)
    finally:
        drop(pg_dsn, ns)
    assert result.disagreements, f"SQL mutant {name} survived"
    assert not result.ok


def _active_ignoring_login(alias: str, role: str) -> str:
    return (f"{alias}.role = {sqlgen.ql(role)} AND {alias}.valid_from <= c.today "
            f"AND ({alias}.valid_to IS NULL OR c.today <= {alias}.valid_to)")


def _drop_assignment_rls(s: str) -> bool:
    return not ("assignments" in s and ("ROW LEVEL SECURITY" in s or "own_rows" in s))


@pytest.mark.parametrize("variant", ["login_clause_only", "assignment_rls_only", "both"])
def test_login_binding_has_two_layers(variant: str, pg_dsn: str, instance_dir: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """The policy's ``login = current_user`` and the RLS on eeb_sec.assignments each bind
    an assignment to the asking login. Removing either alone is masked by the other (a
    paired mutant, not a survivor); removing both must be detected."""
    if variant in ("login_clause_only", "both"):
        monkeypatch.setattr(sqlgen, "_active_clause", _active_ignoring_login)
    if variant in ("assignment_rls_only", "both"):
        monkeypatch.setattr(sqlgen, "security_sql", _filter_security(_drop_assignment_rls))
    ns = unique_ns("mutpair")
    build_database(pg_dsn, instance_dir, ns)
    try:
        result = agreement.check(pg_dsn, instance_dir, ns)
    finally:
        drop(pg_dsn, ns)
    if variant == "both":
        assert result.disagreements, "paired login-binding mutant survived"
    else:
        assert result.disagreements == [], f"{variant}: the remaining layer should hold"


def test_rls_disabled_on_one_table_is_detected(pg_dsn: str, instance_dir: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlgen, "security_sql", _filter_security(
        lambda s: not ("ROW LEVEL SECURITY" in s and '"purchase_orders"' in s)))
    ns = unique_ns("mutrls")
    build_database(pg_dsn, instance_dir, ns)
    try:
        result = agreement.check(pg_dsn, instance_dir, ns)
    finally:
        drop(pg_dsn, ns)
    assert result.disagreements and result.hardening


# ---------------------------------------------------------------- oracle mutants
def _db(pg_dsn: str, ns: str, instance_dir: Path) -> dict[str, dict[str, Any]]:
    pids = [json.loads(x)["principal_id"]
            for x in (instance_dir / "principals.jsonl").read_text().splitlines()]
    return agreement.db_outcome(pg_dsn, ns, pids)


def _active_ignoring_valid_to(self: Oracle, pid: str) -> list[Any]:
    return [a for a in self._assignments
            if a["principal_id"] == pid and a["valid_from"] <= self._today]


def _holds_null_parent_true(orig: Callable[..., bool]) -> Callable[..., bool]:
    def f(self: Oracle, rule: Any, row: Any, param: Any, pid: str) -> bool:
        if isinstance(rule, dict) and "in_parent" in rule and any(
                row[c] is None for c in rule["in_parent"]["fk"]):
            return True
        return orig(self, rule, row, param, pid)
    return f


def _holds_in_values_any(orig: Callable[..., bool]) -> Callable[..., bool]:
    def f(self: Oracle, rule: Any, row: Any, param: Any, pid: str) -> bool:
        if isinstance(rule, dict) and "in_values" in rule:
            return True
        return orig(self, rule, row, param, pid)
    return f


ORACLE_MUTANTS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "active_ignores_valid_to": lambda mp: mp.setattr(Oracle, "active", _active_ignoring_valid_to),
    "null_fk_parent_visible": lambda mp: mp.setattr(Oracle, "_holds",
                                                    _holds_null_parent_true(Oracle._holds)),
    "in_values_ignored": lambda mp: mp.setattr(Oracle, "_holds",
                                               _holds_in_values_any(Oracle._holds)),
    "star_columns_only_pk": lambda mp: mp.setattr(
        Oracle, "columns", lambda self, pid, t: sorted(
            __import__("eeb.schema", fromlist=["BY_NAME"]).BY_NAME[t].pk)
        if self.privileged(pid, t) else []),
    "privileged_ignores_activity": lambda mp: mp.setattr(
        Oracle, "privileged", lambda self, pid, t: any(
            t in self._roles[a["role"]]["tables"] for a in self._assignments
            if a["principal_id"] == pid)),
}


@pytest.mark.parametrize("name", sorted(ORACLE_MUTANTS))
def test_oracle_mutant_is_detected(name: str, pg_dsn: str, built_db: str, instance_dir: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(pg_dsn, built_db, instance_dir)
    ORACLE_MUTANTS[name](monkeypatch)
    ora = agreement.oracle_outcome(instance_dir)
    assert agreement.compare(db, ora), f"oracle mutant {name} survived"


def test_dropping_force_rls_is_caught_by_hardening(pg_dsn: str, instance_dir: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    # Not observable through visibility (tables are owned by the superuser admin, which
    # bypasses RLS anyway, and principals own nothing), so the hardening check must catch it.
    monkeypatch.setattr(sqlgen, "security_sql", _filter_security(
        lambda s: not ("FORCE ROW LEVEL SECURITY" in s and '"budgets"' in s)))
    ns = unique_ns("mutforce")
    build_database(pg_dsn, instance_dir, ns)
    try:
        result = agreement.check(pg_dsn, instance_dir, ns)
    finally:
        drop(pg_dsn, ns)
    assert any("budgets" in h for h in result.hardening) and not result.ok


# Equivalent mutant, not killable on this policy and therefore not counted:
# - eq_param "IS NOT DISTINCT FROM" instead of "=": every parameterised assignment has a
#   non-null parameter (enforced by validate_principals), so NULL = NULL never arises.
