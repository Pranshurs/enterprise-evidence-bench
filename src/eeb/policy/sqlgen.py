"""Policy → Postgres enforcement (implementation 1 of 2; spec §5.2).

Compiles the raw policy mapping into roles, grants and row-level-security policies.
The SQL is evaluated by Postgres itself, so visibility on this path is decided by the
database engine, never by Python. This module shares no code with ``oracle.py``.

Role names are namespaced (Postgres roles are cluster-global) so that independent
databases on one server never share principals.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from eeb.schema import BY_NAME, SCHEMA, SEC_SCHEMA


def qi(name: str) -> str:
    """Quote an identifier (names are validated upstream; quoting is defence in depth)."""
    return '"' + name.replace('"', '""') + '"'


def ql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def group_role(ns: str, role: str) -> str:
    return f"{ns}_g_{role}"


def principal_group(ns: str) -> str:
    return f"{ns}_g_principal"


def login_role(ns: str, principal_id: str) -> str:
    return f"{ns}_p_{principal_id}"


def login_password(ns: str, principal_id: str) -> str:
    # Local fixture credential for an ephemeral benchmark database; not a secret.
    return f"{ns}-{principal_id}-local"


def _active_clause(alias: str, role: str) -> str:
    return (f"{alias}.login = current_user AND {alias}.role = {ql(role)} "
            f"AND {alias}.valid_from <= c.today "
            f"AND ({alias}.valid_to IS NULL OR c.today <= {alias}.valid_to)")


def _pred(rule: Any, table: str, depth: int = 0) -> str:
    if rule == "all":
        return "TRUE"
    (op, arg), = rule.items()
    target = f"{qi(SCHEMA)}.{qi(table)}"
    if op == "eq_param":
        return f"({target}.{qi(arg['column'])} = a.param_value)"
    if op == "in_values":
        vals = ", ".join(ql(v) for v in arg["values"])
        return f"({target}.{qi(arg['column'])} IN ({vals}))"
    if op == "is_null":
        return f"({target}.{qi(arg)} IS NULL)"
    if op == "in_parent":
        alias = f"par{depth}"
        cond = " AND ".join(f"{alias}.{qi(p)} = {target}.{qi(f)}"
                            for f, p in zip(arg["fk"], arg["pk"], strict=True))
        return f"(EXISTS (SELECT 1 FROM {qi(SCHEMA)}.{qi(arg['table'])} {alias} WHERE {cond}))"
    joiner = " OR " if op == "any_of" else " AND "
    return "(" + joiner.join(_pred(sub, table, depth + 1) for sub in arg) + ")"


def policy_using(role: str, table: str, rule: Any) -> str:
    """USING clause: some active assignment of ``role`` for current_user satisfies ``rule``.

    The rule is evaluated per assignment (``a.param_value``), matching the written policy's
    per-assignment semantics.
    """
    return (f"EXISTS (SELECT 1 FROM {qi(SEC_SCHEMA)}.assignments a "
            f"CROSS JOIN {qi(SEC_SCHEMA)}.clock c WHERE {_active_clause('a', role)} "
            f"AND {_pred(rule, table)})")


def _is_active(a: dict[str, Any], today: dt.date) -> bool:
    return a["valid_from"] <= today and (a["valid_to"] is None or today <= a["valid_to"])


def security_sql(ns: str, dbname: str, policy: dict[str, Any], principals: list[dict[str, Any]],
                 assignments: list[dict[str, Any]], today: dt.date) -> list[str]:
    """Statements to run as the database owner after tables are created and loaded."""
    out: list[str] = []
    pg = principal_group(ns)
    out.append(f"CREATE ROLE {qi(pg)} NOLOGIN")
    roles = policy["roles"]
    for role in roles:
        out.append(f"CREATE ROLE {qi(group_role(ns, role))} NOLOGIN")
    out.append(f"REVOKE ALL ON DATABASE {qi(dbname)} FROM PUBLIC")
    out.append(f"GRANT CONNECT ON DATABASE {qi(dbname)} TO {qi(pg)}")
    out.append("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    out.append(f"GRANT USAGE ON SCHEMA {qi(SCHEMA)} TO {qi(pg)}")
    out.append(f"GRANT USAGE ON SCHEMA {qi(SEC_SCHEMA)} TO {qi(pg)}")

    # Security tables: the clock is shared; each login sees only its own assignment rows.
    out.append(f"GRANT SELECT ON {qi(SEC_SCHEMA)}.clock TO {qi(pg)}")
    out.append(f"GRANT SELECT ON {qi(SEC_SCHEMA)}.assignments TO {qi(pg)}")
    out.append(f"ALTER TABLE {qi(SEC_SCHEMA)}.assignments ENABLE ROW LEVEL SECURITY")
    out.append(f"ALTER TABLE {qi(SEC_SCHEMA)}.assignments FORCE ROW LEVEL SECURITY")
    out.append(f"CREATE POLICY own_rows ON {qi(SEC_SCHEMA)}.assignments AS PERMISSIVE FOR SELECT "
               f"TO {qi(pg)} USING (login = current_user)")

    # Every data table gets RLS, whether or not any role is granted it (deny by default).
    for tname in BY_NAME:
        out.append(f"ALTER TABLE {qi(SCHEMA)}.{qi(tname)} ENABLE ROW LEVEL SECURITY")
        out.append(f"ALTER TABLE {qi(SCHEMA)}.{qi(tname)} FORCE ROW LEVEL SECURITY")
    for role, spec in roles.items():
        gr = qi(group_role(ns, role))
        for tname, entry in spec["tables"].items():
            target = f"{qi(SCHEMA)}.{qi(tname)}"
            if entry["columns"] == "*":
                out.append(f"GRANT SELECT ON {target} TO {gr}")
            else:
                cols = ", ".join(qi(c) for c in entry["columns"])
                out.append(f"GRANT SELECT ({cols}) ON {target} TO {gr}")
            out.append(f"CREATE POLICY {qi(role + '__select')} ON {target} AS PERMISSIVE "
                       f"FOR SELECT TO {gr} USING ({policy_using(role, tname, entry['rows'])})")

    # Logins: one per principal; group membership only for roles active today.
    for p in principals:
        login = login_role(ns, p["principal_id"])
        password = ql(login_password(ns, p["principal_id"]))
        out.append(f"CREATE ROLE {qi(login)} LOGIN PASSWORD {password} "
                   "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT "
                   "CONNECTION LIMIT 8")
        out.append(f"GRANT {qi(pg)} TO {qi(login)}")
        active_roles = sorted({a["role"] for a in assignments
                               if a["principal_id"] == p["principal_id"] and _is_active(a, today)})
        for role in active_roles:
            out.append(f"GRANT {qi(group_role(ns, role))} TO {qi(login)}")
    return out
