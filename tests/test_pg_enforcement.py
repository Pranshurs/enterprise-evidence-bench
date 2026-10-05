"""Postgres enforcement: agreement with the oracle, fresh rebuilds, adversarial probes,
and an independent derivation of the SQL gold facts."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from eeb.db import agreement
from eeb.db.load import admin, build_database, drop, dsn_for, export_tables
from eeb.policy import sqlgen
from tests.conftest import unique_ns
from tests.expectations import NEVER_COLUMNS, NEVER_GRANTED, NO_ACCESS, ROW_EXPECTATIONS

pytestmark = pytest.mark.pg


@contextmanager
def as_principal(pg_dsn: str, ns: str, pid: str) -> Iterator[psycopg.Connection[Any]]:
    dsn = dsn_for(pg_dsn, ns, sqlgen.login_role(ns, pid), sqlgen.login_password(ns, pid))
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


def test_database_and_oracle_agree_exhaustively(pg_dsn: str, built_db: str,
                                                instance_dir: Path) -> None:
    result = agreement.check(pg_dsn, instance_dir, built_db)
    assert result.disagreements == []
    assert result.hardening == []
    assert result.db_digest == result.oracle_digest == result.recorded_digest
    assert result.ok


def test_reload_is_byte_identical(pg_dsn: str, built_db: str, files: dict[str, bytes]) -> None:
    exported = export_tables(pg_dsn, built_db)
    assert [k for k, v in exported.items() if v != files[k]] == []


def test_fresh_rebuild_reproduces_authorization_outcomes(pg_dsn: str, built_db: str,
                                                         instance_dir: Path) -> None:
    principals = [json.loads(x)["principal_id"]
                  for x in (instance_dir / "principals.jsonl").read_text().splitlines()]
    first = agreement.db_outcome(pg_dsn, built_db, principals)
    ns = unique_ns("rebuild")
    build_database(pg_dsn, instance_dir, ns)
    try:
        second = agreement.db_outcome(pg_dsn, ns, principals)
        assert agreement.compare(first, second) == []
        assert export_tables(pg_dsn, ns) == export_tables(pg_dsn, built_db)
    finally:
        drop(pg_dsn, ns)


@pytest.mark.parametrize("pid", sorted(ROW_EXPECTATIONS))
def test_database_rows_match_prose_expectations(pg_dsn: str, built_db: str, pid: str,
                                                tables: dict[str, list[dict[str, Any]]]) -> None:
    from eeb.schema import BY_NAME

    with as_principal(pg_dsn, built_db, pid) as conn:
        for table, expected in ROW_EXPECTATIONS[pid](tables).items():
            pk = ", ".join(BY_NAME[table].pk)
            got = {tuple(str(v) for v in r) for r in conn.execute(f"SELECT {pk} FROM eeb.{table}")}
            assert got == expected, (pid, table)


@pytest.mark.parametrize("pid", NO_ACCESS)
def test_inactive_principals_are_denied_by_the_database(pg_dsn: str, built_db: str,
                                                        pid: str) -> None:
    with as_principal(pg_dsn, built_db, pid) as conn:
        for table in ("suppliers", "purchase_orders", "doc_chunks", "invoices"):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(f"SELECT 1 FROM eeb.{table} LIMIT 1")


ALL_ACTIVE = ("cm_met", "cm_multi", "cm_moved", "buyer_in", "ap_eu", "fin_ctrl", "legal", "risk")


@pytest.mark.parametrize("pid", ALL_ACTIVE)
def test_never_granted_objects_are_denied(pg_dsn: str, built_db: str, pid: str) -> None:
    with as_principal(pg_dsn, built_db, pid) as conn:
        for table in NEVER_GRANTED:
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(f"SELECT * FROM eeb.{table}")
        for table, cols in NEVER_COLUMNS.items():
            for col in cols:
                with pytest.raises(errors.InsufficientPrivilege):
                    conn.execute(f"SELECT {col} FROM eeb.{table}")


@pytest.mark.parametrize("stmt", [
    "INSERT INTO eeb.suppliers (supplier_id) VALUES ('X')",
    "UPDATE eeb.suppliers SET name = 'x'",
    "DELETE FROM eeb.purchase_orders",
    "TRUNCATE eeb.po_lines",
    "UPDATE eeb_sec.clock SET today = '2025-01-01'",
    "INSERT INTO eeb_sec.assignments VALUES "
    "('x','x','finance_controller',NULL,NULL,'2020-01-01',NULL)",
    "CREATE TABLE public.t (x int)",
    "CREATE TEMP TABLE t (x int)",
    "CREATE SCHEMA evil",
])
def test_writes_and_ddl_are_denied(pg_dsn: str, built_db: str, stmt: str) -> None:
    with as_principal(pg_dsn, built_db, "cm_met") as conn, pytest.raises(
            errors.InsufficientPrivilege):
        conn.execute(stmt)


# RLS-reached cases: the principal HAS relation privilege (the query succeeds), and the
# database still returns a strict, non-empty subset equal to the policy. These cannot pass
# by outer privilege denial. Privilege-denial cases are tested separately above.
RLS_REACHED = [
    ("cm_met", "suppliers"), ("cm_met", "purchase_orders"), ("cm_met", "po_lines"),
    ("cm_met", "invoices"), ("cm_met", "doc_chunks"), ("cm_moved", "suppliers"),
    ("cm_multi", "contracts"), ("buyer_in", "purchase_orders"), ("buyer_eu", "goods_receipts"),
    ("ap_eu", "invoices"), ("ap_uk", "payments"), ("fin_ctrl", "doc_chunks"),
    ("legal", "doc_chunks"), ("risk", "doc_chunks"),
]


@pytest.mark.parametrize("pid,table", RLS_REACHED)
def test_rls_is_reached_and_filters(pg_dsn: str, built_db: str, pid: str, table: str,
                                    oracle: Any) -> None:
    from eeb.schema import BY_NAME

    pk = ", ".join(BY_NAME[table].pk)
    with admin(pg_dsn, built_db) as adm:
        total = adm.execute(f"SELECT count(*) FROM eeb.{table}").fetchone()[0]  # type: ignore[index]
        login = sqlgen.login_role(built_db, pid)
        assert adm.execute("SELECT has_table_privilege(%s, %s, 'SELECT') OR "
                           "has_column_privilege(%s, %s, %s, 'SELECT')",
                           (login, f"eeb.{table}", login, f"eeb.{table}",
                            BY_NAME[table].pk[0])).fetchone()[0]  # type: ignore[index]
    with as_principal(pg_dsn, built_db, pid) as conn:
        got = {tuple(str(v) for v in r) for r in conn.execute(f"SELECT {pk} FROM eeb.{table}")}
    assert 0 < len(got) < total, (pid, table, len(got), total)
    assert got == set(oracle.visible_rows(pid, table))


def test_role_switching_cannot_escalate(pg_dsn: str, built_db: str) -> None:
    ns = built_db
    with as_principal(pg_dsn, ns, "cm_met") as conn:
        for target in (sqlgen.group_role(ns, "finance_controller"),
                       sqlgen.login_role(ns, "fin_ctrl")):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(f'SET ROLE "{target}"')
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(f'SET SESSION AUTHORIZATION "{sqlgen.login_role(ns, "fin_ctrl")}"')
        # Switching to its own group role drops the login identity. This proves a safe
        # PRIVILEGE denial (the group role has no schema usage), not the RLS predicate; RLS
        # filtering is proven separately by test_rls_is_reached_and_filters.
        conn.execute(f'SET ROLE "{sqlgen.group_role(ns, "category_manager")}"')
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM eeb.suppliers")
        conn.execute("RESET ROLE")
        assert conn.execute("SELECT count(*) FROM eeb.suppliers").fetchone()[0] > 0  # type: ignore[index]


def test_assignment_table_shows_only_own_rows(pg_dsn: str, built_db: str) -> None:
    with as_principal(pg_dsn, built_db, "cm_moved") as conn:
        rows = conn.execute("SELECT principal_id, param_value FROM eeb_sec.assignments "
                            "ORDER BY param_value").fetchall()
    assert rows == [("cm_moved", "CAT-ITH"), ("cm_moved", "CAT-MRO")]


def test_session_settings_cannot_move_the_clock(pg_dsn: str, built_db: str,
                                                tables: dict[str, list[dict[str, Any]]]) -> None:
    mro = {s["supplier_id"] for s in tables["suppliers"] if s["category_id"] == "CAT-MRO"}
    with as_principal(pg_dsn, built_db, "cm_moved") as conn:
        conn.execute("SET TimeZone = 'Pacific/Kiritimati'")
        conn.execute("SELECT set_config('eeb.today', '2025-01-01', false)")
        seen = {r[0] for r in conn.execute("SELECT supplier_id FROM eeb.suppliers")}
    assert seen and not (seen & mro)


# ---------------------------------------------------------------- gold facts, derived twice
def _num(x: Any) -> Decimal:
    return Decimal(str(x))


def test_sql_gold_facts_match_an_independent_sql_derivation(pg_dsn: str, built_db: str,
                                                            files: dict[str, bytes]) -> None:
    facts = [json.loads(x) for x in files["gold/scenario_facts.jsonl"].decode().splitlines()]
    sql_facts = [f for f in facts if f["source"] == "sql"]
    assert len(sql_facts) >= 8
    with admin(pg_dsn, built_db) as conn:
        for f in sql_facts:
            rows = conn.execute(f["gold_sql"]).fetchall()
            if f["kind"] == "entity_set":
                assert [r[0] for r in rows] == f["value"], f["fact_id"]
            elif f["kind"] == "boolean":
                assert rows[0][0] is f["value"], f["fact_id"]
            else:
                diff = abs(_num(rows[0][0]) - _num(f["value"]))
                assert diff <= _num(f["tolerance"]), (f["fact_id"], rows[0][0], f["value"])


def test_derived_gold_facts_recompute(files: dict[str, bytes]) -> None:
    facts = {f["fact_id"]: f for f in
             (json.loads(x) for x in files["gold/scenario_facts.jsonl"].decode().splitlines())}
    v = {k: _num(f["value"]) for k, f in facts.items()
         if f["kind"] in ("number", "money") and not isinstance(f["value"], list)}
    derived = [f for f in facts.values() if f["source"] == "derived"]
    assert derived
    for f in derived:
        ins = [v[i] for i in f["derived"]["inputs"]]
        op = f["derived"]["op"]
        if op == "pct_change":
            want = (ins[1] - ins[0]) / ins[0] * 100
        elif op == "floor_shortfall":
            want = Decimal(max(0, math.floor(ins[0] - ins[1])))
        elif op == "min_mul_cap":
            want = min(ins[0] * ins[1], ins[2])
        elif op == "pct_of":
            want = ins[1] * ins[0] / 100
        else:
            raise AssertionError(op)
        assert abs(want - _num(f["value"])) <= max(_num(f["tolerance"]), Decimal("0.01")), f


def test_build_gate_gold_check_detects_a_wrong_gold_value(pg_dsn: str, built_db: str,
                                                          instance_dir: Path,
                                                          tmp_path: Path) -> None:
    import shutil

    from eeb.db.gold_check import check_gold_sql

    assert check_gold_sql(pg_dsn, instance_dir, built_db)["mismatches"] == []
    bad = tmp_path / "bad"
    shutil.copytree(instance_dir, bad)
    path = bad / "gold/scenario_facts.jsonl"
    facts = [json.loads(x) for x in path.read_text().splitlines()]
    target = next(f for f in facts if f["source"] == "sql" and f["kind"] == "number")
    target["value"] = str(Decimal(str(target["value"])) + 1)
    path.write_text("".join(json.dumps(f) + "\n" for f in facts))
    out = check_gold_sql(pg_dsn, bad, built_db)
    assert [m["fact_id"] for m in out["mismatches"]] == [target["fact_id"]]
