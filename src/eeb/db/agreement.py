"""Exhaustive agreement between Postgres enforcement and the Python oracle (spec §5.2).

The database side is measured by *connecting as each principal's login* and reading every
table: what Postgres returns is what the engine enforced. Column visibility comes from
``has_column_privilege``. The oracle side is computed from the instance files alone. Any
difference in any (principal, table, privileged | columns | rows) cell is a disagreement.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg import errors

from eeb.db.load import admin, dsn_for, parsed_assignments, read_instance, read_jsonl
from eeb.generator.instance import summarize_outcome
from eeb.policy import schema as pschema
from eeb.policy import sqlgen
from eeb.policy.oracle import Oracle
from eeb.schema import SCHEMA, SEC_SCHEMA, TABLES


def _key(v: Any) -> str:
    return v.isoformat() if isinstance(v, dt.date) else str(v)


def db_outcome(admin_dsn: str, ns: str, principal_ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with admin(admin_dsn, ns) as adm:
        for pid in principal_ids:
            login = sqlgen.login_role(ns, pid)
            per: dict[str, Any] = {}
            dsn = dsn_for(admin_dsn, ns, login, sqlgen.login_password(ns, pid))
            with psycopg.connect(dsn, autocommit=True) as conn:
                for t in TABLES:
                    pk = ", ".join(sqlgen.qi(c) for c in t.pk)
                    try:
                        rows = conn.execute(f"SELECT {pk} FROM {SCHEMA}.{sqlgen.qi(t.name)}")
                        keys = sorted([_key(v) for v in r] for r in rows)
                        privileged = True
                    except errors.InsufficientPrivilege:
                        keys, privileged = [], False
                    cols = [c for c in t.column_names if adm.execute(
                        "SELECT has_column_privilege(%s, %s, %s, 'SELECT')",
                        (login, f"{SCHEMA}.{t.name}", c)).fetchone()[0]]  # type: ignore[index]
                    per[t.name] = {"privileged": privileged, "columns": sorted(cols),
                                   "rows": keys}
            out[pid] = per
    return out


def oracle_outcome(instance: Path) -> dict[str, dict[str, Any]]:
    meta = read_instance(instance)
    policy = pschema.load_policy((instance / "policy.yaml").read_bytes())
    tables = {}
    from eeb.db.load import _parse

    for t in TABLES:
        recs = read_jsonl(instance / f"tables/{t.name}.jsonl")
        tables[t.name] = [dict(zip(t.column_names, _parse(t, r), strict=True)) for r in recs]
    principals = [p["principal_id"] for p in read_jsonl(instance / "principals.jsonl")]
    oracle = Oracle(policy, tables, parsed_assignments(instance),
                    dt.date.fromisoformat(meta["config"]["today"]))
    return oracle.outcome(principals)


@dataclass
class Agreement:
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    hardening: list[str] = field(default_factory=list)
    db_digest: str = ""
    oracle_digest: str = ""
    recorded_digest: str = ""

    @property
    def ok(self) -> bool:
        return (not self.disagreements and not self.hardening
                and self.db_digest == self.oracle_digest == self.recorded_digest)


def compare(db: dict[str, dict[str, Any]], ora: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    for pid in sorted(set(db) | set(ora)):
        for t in TABLES:
            d, o = db.get(pid, {}).get(t.name), ora.get(pid, {}).get(t.name)
            if d is None or o is None:
                diffs.append({"principal": pid, "table": t.name, "field": "missing"})
                continue
            for fld in ("privileged", "columns"):
                if d[fld] != o[fld]:
                    diffs.append({"principal": pid, "table": t.name, "field": fld,
                                  "db": d[fld], "oracle": o[fld]})
            if d["rows"] != o["rows"]:
                ds, os_ = {tuple(r) for r in d["rows"]}, {tuple(r) for r in o["rows"]}
                diffs.append({"principal": pid, "table": t.name, "field": "rows",
                              "db_only": sorted(ds - os_)[:5], "oracle_only": sorted(os_ - ds)[:5],
                              "db_only_count": len(ds - os_), "oracle_only_count": len(os_ - ds)})
    return diffs


def hardening_checks(admin_dsn: str, ns: str, principal_ids: list[str]) -> list[str]:
    """Structural properties the enforcement claim depends on."""
    problems: list[str] = []
    with admin(admin_dsn, ns) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s", (SCHEMA,))}
        if tables != {t.name for t in TABLES}:
            extra = sorted(tables ^ {t.name for t in TABLES})
            problems.append(f"schema tables differ from definition: {extra}")
        for name, rls, force in conn.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s "
                "AND c.relkind = 'r'", (SCHEMA,)):
            if not (rls and force):
                problems.append(f"{name}: row level security not enabled and forced")
        for pid in principal_ids:
            login = sqlgen.login_role(ns, pid)
            row = conn.execute(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
                "FROM pg_roles WHERE rolname = %s", (login,)).fetchone()
            if row is None:
                problems.append(f"{login}: login missing")
                continue
            if any(row):
                problems.append(f"{login}: has a privileged attribute {row}")
            owned = conn.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "WHERE r.rolname = %s", (login,)).fetchone()
            if owned and owned[0]:
                problems.append(f"{login}: owns relations")
            for (other,) in conn.execute(
                    "SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
                    "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s", (login,)):
                if not other.startswith(ns + "_g_"):
                    problems.append(f"{login}: member of foreign role {other}")
            for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                for t in TABLES:
                    if conn.execute("SELECT has_table_privilege(%s, %s, %s)",
                                    (login, f"{SCHEMA}.{t.name}", priv)).fetchone()[0]:  # type: ignore[index]
                        problems.append(f"{login}: {priv} on {t.name}")
            for sec in ("assignments", "clock"):
                for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    if conn.execute("SELECT has_table_privilege(%s, %s, %s)",
                                    (login, f"{SEC_SCHEMA}.{sec}", priv)).fetchone()[0]:  # type: ignore[index]
                        problems.append(f"{login}: {priv} on {SEC_SCHEMA}.{sec}")
            if conn.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')",
                            (login,)).fetchone()[0]:  # type: ignore[index]
                problems.append(f"{login}: CREATE on schema public")
    return problems


def check(admin_dsn: str, instance: Path, ns: str) -> Agreement:
    meta = read_instance(instance)
    principals = [p["principal_id"] for p in read_jsonl(instance / "principals.jsonl")]
    db = db_outcome(admin_dsn, ns, principals)
    ora = oracle_outcome(instance)
    return Agreement(
        disagreements=compare(db, ora),
        hardening=hardening_checks(admin_dsn, ns, principals),
        db_digest=summarize_outcome(db)["digest"],
        oracle_digest=summarize_outcome(ora)["digest"],
        recorded_digest=meta["authorization_outcome_digest"],
    )
