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


def db_outcome(admin_dsn: str, ns: str, principal_ids: list[str],
               verifier: bool = False) -> dict[str, dict[str, Any]]:
    """Measured visibility per principal (``verifier=True`` measures the harness twins)."""
    out: dict[str, dict[str, Any]] = {}
    with admin(admin_dsn, ns) as adm:
        for pid in principal_ids:
            if verifier:
                login, pw = sqlgen.verifier_role(ns, pid), sqlgen.verifier_password(ns, pid)
            else:
                login, pw = sqlgen.login_role(ns, pid), sqlgen.login_password(ns, pid)
            per: dict[str, Any] = {}
            dsn = dsn_for(admin_dsn, ns, login, pw)
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
    twin_disagreements: list[dict[str, Any]] = field(default_factory=list)
    twin_digest: str = ""
    service_problems: list[str] = field(default_factory=list)
    service_digest: str = ""

    @property
    def ok(self) -> bool:
        return (not self.disagreements and not self.hardening
                and not self.twin_disagreements and not self.service_problems
                and self.db_digest == self.oracle_digest == self.recorded_digest
                == self.twin_digest)


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
        logins = [f(ns, pid) for pid in principal_ids
                  for f in (sqlgen.login_role, sqlgen.verifier_role)] + [sqlgen.service_role(ns)]
        for login in logins:
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


def service_outcome(admin_dsn: str, ns: str) -> dict[str, Any]:
    """Measured Mode-S visibility, in the same shape as a principal's outcome."""
    login, pw = sqlgen.service_role(ns), sqlgen.service_password(ns)
    per: dict[str, Any] = {}
    with admin(admin_dsn, ns) as adm, psycopg.connect(
            dsn_for(admin_dsn, ns, login, pw), autocommit=True) as conn:
        for t in TABLES:
            pk = ", ".join(sqlgen.qi(c) for c in t.pk)
            try:
                rows = conn.execute(f"SELECT {pk} FROM {SCHEMA}.{sqlgen.qi(t.name)}")
                keys, privileged = sorted([_key(v) for v in r] for r in rows), True
            except errors.InsufficientPrivilege:
                keys, privileged = [], False
            cols = [c for c in t.column_names if adm.execute(
                "SELECT has_column_privilege(%s, %s, %s, 'SELECT')",
                (login, f"{SCHEMA}.{t.name}", c)).fetchone()[0]]  # type: ignore[index]
            per[t.name] = {"privileged": privileged, "columns": sorted(cols), "rows": keys}
    return per


def expected_service_outcome(instance: Path) -> dict[str, Any]:
    """Mode-S expectation from the policy file: every row of the union of role grants."""
    from eeb.db.load import _parse
    from eeb.policy.oracle import pk_key

    policy = pschema.load_policy((instance / "policy.yaml").read_bytes())
    grants = sqlgen.service_grants(policy)
    per: dict[str, Any] = {}
    for t in TABLES:
        if t.name not in grants:
            per[t.name] = {"privileged": False, "columns": [], "rows": []}
            continue
        recs = read_jsonl(instance / f"tables/{t.name}.jsonl")
        rows = sorted(list(pk_key(t.name, dict(zip(t.column_names, _parse(t, r), strict=True))))
                      for r in recs)
        per[t.name] = {"privileged": True, "columns": sorted(grants[t.name]), "rows": rows}
    return per


def logging_checks(admin_dsn: str, ns: str, principal_ids: list[str]) -> list[str]:
    """Statement-observation configuration the DB-side evidence depends on (ADR-0006)."""
    problems: list[str] = []
    with admin(admin_dsn, ns) as conn:
        def show(name: str) -> str:
            return str(conn.execute(f"SHOW {name}").fetchone()[0])  # type: ignore[index]

        if show("logging_collector") != "on":
            problems.append("logging_collector is not on")
        if "jsonlog" not in show("log_destination"):
            problems.append("log_destination does not include jsonlog")
        if show("log_statement") != "none":
            problems.append("server-wide log_statement must be none (harness statements "
                            "would be logged)")

        def config(role: str) -> list[str]:
            row = conn.execute("SELECT rolconfig FROM pg_roles WHERE rolname = %s",
                               (role,)).fetchone()
            return list(row[0] or []) if row else []

        for pid in principal_ids:
            if "log_statement=all" not in config(sqlgen.login_role(ns, pid)):
                problems.append(f"{pid}: SUT login is not statement-logged")
            if any(c.startswith("log_statement") for c in config(sqlgen.verifier_role(ns, pid))):
                problems.append(f"{pid}: verifier twin must not be statement-logged")
        if "log_statement=all" not in config(sqlgen.service_role(ns)):
            problems.append("service login is not statement-logged")
    return problems


def metric_layer_checks(admin_dsn: str, ns: str) -> list[str]:
    """The metric layer can never act with more than the caller's authority."""
    from eeb.metrics.layer import METRIC_SCHEMA, VIEWS, owner_role

    problems: list[str] = []
    with admin(admin_dsn, ns) as conn:
        rows = conn.execute(
            "SELECT c.relname, c.relkind, c.reloptions, pg_get_userbyid(c.relowner) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s", (METRIC_SCHEMA,)).fetchall()
        names = {r[0] for r in rows}
        if names != set(VIEWS):
            problems.append(f"{METRIC_SCHEMA} objects differ from the declared views: "
                            f"{sorted(names ^ set(VIEWS))}")
        for name, kind, options, owner in rows:
            if kind != "v":
                problems.append(f"{name}: not a view")
            if "security_invoker=true" not in (options or []):
                problems.append(f"{name}: not security_invoker")
            if owner != owner_role(ns):
                problems.append(f"{name}: owned by {owner}")
        attrs = conn.execute("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
                             "WHERE rolname = %s", (owner_role(ns),)).fetchone()
        if attrs is None or any(attrs):
            problems.append(f"metric owner role has privileged attributes: {attrs}")
        for t in TABLES:
            if conn.execute("SELECT has_table_privilege(%s, %s, 'SELECT')",
                            (owner_role(ns), f"{SCHEMA}.{t.name}")).fetchone()[0]:  # type: ignore[index]
                problems.append(f"metric owner can read {t.name}")
        definers = conn.execute(
            "SELECT n.nspname || '.' || p.proname FROM pg_proc p JOIN pg_namespace n "
            "ON n.oid = p.pronamespace WHERE p.prosecdef AND n.nspname NOT IN "
            "('pg_catalog', 'information_schema')").fetchall()
        if definers:
            problems.append(f"SECURITY DEFINER functions present: {[d[0] for d in definers]}")
    return problems


def check(admin_dsn: str, instance: Path, ns: str) -> Agreement:
    meta = read_instance(instance)
    principals = [p["principal_id"] for p in read_jsonl(instance / "principals.jsonl")]
    db = db_outcome(admin_dsn, ns, principals)
    twins = db_outcome(admin_dsn, ns, principals, verifier=True)
    ora = oracle_outcome(instance)
    svc, svc_expected = service_outcome(admin_dsn, ns), expected_service_outcome(instance)
    svc_problems = [f"{t}: {k}" for t in svc for k in ("privileged", "columns", "rows")
                    if svc[t][k] != svc_expected[t][k]]
    return Agreement(
        disagreements=compare(db, ora),
        hardening=hardening_checks(admin_dsn, ns, principals)
        + logging_checks(admin_dsn, ns, principals) + metric_layer_checks(admin_dsn, ns),
        db_digest=summarize_outcome(db)["digest"],
        oracle_digest=summarize_outcome(ora)["digest"],
        recorded_digest=meta["authorization_outcome_digest"],
        twin_disagreements=compare(twins, db),
        twin_digest=summarize_outcome(twins)["digest"],
        service_problems=svc_problems,
        service_digest=summarize_outcome({"service": svc})["digest"],
    )
