"""Build a fresh Postgres database from an instance directory.

The admin DSN must reach a server where the admin may create databases and roles (a local
fixture). Every database is fresh: there is no incremental update path. The namespace
(``ns``) is the database name, and every role created for it is prefixed with it.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from eeb import canonical
from eeb.policy import schema as pschema
from eeb.policy import sqlgen
from eeb.schema import SCHEMA, SEC_SCHEMA, TABLES, Table

_PG_TYPE = {"text": "text", "int": "integer", "numeric": "numeric", "date": "date",
            "bool": "boolean"}


class InstanceIntegrityError(RuntimeError):
    pass


def read_instance(path: Path) -> dict[str, Any]:
    meta: dict[str, Any] = json.loads((path / "INSTANCE.json").read_text("utf-8"))
    for rel, digest in meta["files"].items():
        if canonical.sha256_bytes((path / rel).read_bytes()) != digest:
            raise InstanceIntegrityError(f"{rel} does not match INSTANCE.json")
    if canonical.digest(meta["files"]) != meta["instance_digest"]:
        raise InstanceIntegrityError("instance digest mismatch")
    return meta


def _parse(t: Table, rec: dict[str, Any]) -> tuple[Any, ...]:
    out: list[Any] = []
    for c in t.columns:
        v = rec[c.name]
        if v is None:
            out.append(None)
        elif c.type == "numeric":
            out.append(Decimal(v))
        elif c.type == "date":
            out.append(dt.date.fromisoformat(v))
        else:
            out.append(v)
    return tuple(out)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def parsed_assignments(instance: Path) -> list[dict[str, Any]]:
    out = []
    for a in read_jsonl(instance / "assignments.jsonl"):
        a = dict(a)
        a["valid_from"] = dt.date.fromisoformat(a["valid_from"])
        a["valid_to"] = dt.date.fromisoformat(a["valid_to"]) if a["valid_to"] else None
        out.append(a)
    return out


def ddl() -> list[str]:
    out = [f"CREATE SCHEMA {SCHEMA}", f"CREATE SCHEMA {SEC_SCHEMA}"]
    for t in TABLES:
        cols = ", ".join(f"{sqlgen.qi(c.name)} {_PG_TYPE[c.type]}"
                         + ("" if c.nullable else " NOT NULL") for c in t.columns)
        pk = ", ".join(sqlgen.qi(c) for c in t.pk)
        out.append(f"CREATE TABLE {SCHEMA}.{sqlgen.qi(t.name)} ({cols}, PRIMARY KEY ({pk}))")
    out.append(f"CREATE TABLE {SEC_SCHEMA}.clock (today date NOT NULL)")
    out.append(f"CREATE TABLE {SEC_SCHEMA}.assignments (login text NOT NULL, principal_id text "
               "NOT NULL, role text NOT NULL, param_name text, param_value text, "
               "valid_from date NOT NULL, valid_to date)")
    return out


def dsn_for(admin_dsn: str, dbname: str, user: str | None = None,
            password: str | None = None) -> str:
    params = conninfo_to_dict(admin_dsn)
    params["dbname"] = dbname
    if user is not None:
        params["user"], params["password"] = user, password
    return make_conninfo(**params)  # type: ignore[arg-type]


@contextmanager
def admin(admin_dsn: str, dbname: str | None = None) -> Iterator[psycopg.Connection[Any]]:
    dsn = admin_dsn if dbname is None else dsn_for(admin_dsn, dbname)
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


def drop(admin_dsn: str, ns: str) -> None:
    with admin(admin_dsn) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {sqlgen.qi(ns)} WITH (FORCE)")
        roles = [r[0] for r in conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname LIKE %s ORDER BY rolname DESC",
            (ns + "\\_%",))]
        for r in roles:
            conn.execute(f"DROP ROLE {sqlgen.qi(r)}")


def build_database(admin_dsn: str, instance: Path, ns: str) -> None:
    """Create database ``ns`` from the instance directory (dropping any previous one)."""
    meta = read_instance(instance)
    policy_raw = (instance / "policy.yaml").read_bytes()
    policy = pschema.load_policy(policy_raw)
    principals = read_jsonl(instance / "principals.jsonl")
    assignments = parsed_assignments(instance)
    pschema.validate_principals(principals, assignments, policy)
    today = dt.date.fromisoformat(meta["config"]["today"])

    drop(admin_dsn, ns)
    with admin(admin_dsn) as conn:
        conn.execute(f"CREATE DATABASE {sqlgen.qi(ns)} TEMPLATE template0 ENCODING 'UTF8' "
                     "LC_COLLATE 'C' LC_CTYPE 'C'")
    with admin(admin_dsn, ns) as conn:
        with conn.transaction():
            for stmt in ddl():
                conn.execute(stmt)
            with conn.cursor() as cur:
                for t in TABLES:
                    cols = ", ".join(sqlgen.qi(c) for c in t.column_names)
                    with cur.copy(f"COPY {SCHEMA}.{sqlgen.qi(t.name)} ({cols}) FROM STDIN") as cp:
                        for rec in read_jsonl(instance / f"tables/{t.name}.jsonl"):
                            cp.write_row(_parse(t, rec))
                cur.execute(f"INSERT INTO {SEC_SCHEMA}.clock (today) VALUES (%s)", (today,))
                for a in assignments:
                    cur.execute(
                        f"INSERT INTO {SEC_SCHEMA}.assignments VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (sqlgen.login_role(ns, a["principal_id"]), a["principal_id"], a["role"],
                         a["param_name"], a["param_value"], a["valid_from"], a["valid_to"]))
            for stmt in sqlgen.security_sql(ns, ns, policy, principals, assignments, today):
                conn.execute(stmt)
        conn.execute("ANALYZE")


def export_tables(admin_dsn: str, ns: str) -> dict[str, bytes]:
    """Canonical JSONL export of every table, ordered by primary key in byte order."""
    out: dict[str, bytes] = {}
    with admin(admin_dsn, ns) as conn:
        for t in TABLES:
            cols = ", ".join(sqlgen.qi(c) for c in t.column_names)
            order = ", ".join(f"{sqlgen.qi(c)} COLLATE \"C\"" if t.column(c).type == "text"
                              else sqlgen.qi(c) for c in t.pk)
            rows = conn.execute(f"SELECT {cols} FROM {SCHEMA}.{sqlgen.qi(t.name)} ORDER BY {order}")
            out[f"tables/{t.name}.jsonl"] = canonical.jsonl(
                dict(zip(t.column_names, r, strict=True)) for r in rows)
    return out
