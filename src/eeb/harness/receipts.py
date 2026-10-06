"""SQL receipt canonicalization and re-execution (spec §8, §9.3; ADR-0006).

Canonical result digest (published, so any SUT can compute it):
1. The result is a **multiset** of rows by default (spec §8: rows sorted). Duplicates and
   NULLs are preserved. When row order is semantically meaningful, the receipt sets
   ``"ordered": true`` and rows keep the order the query returned.
2. Column identity is included: each column's name and PostgreSQL type OID, in select-list
   order.
3. Values are normalized: NULL → null; booleans and integers as JSON; decimals as a
   normalized decimal string (fixed-point); floats via ``repr``; dates and times as
   ISO-8601; bytes as hex; everything else as ``str``.
4. Each row is rendered as compact JSON; for a multiset the rows are sorted by that
   rendering.
5. The digest is SHA-256 of
   ``{"columns": [[name, type_oid], ...], "ordered": bool, "rows": [...]}`` (compact JSON,
   sorted keys).

Re-execution runs under the asking principal's **verifier twin** (harness-only
credentials, same grants and assignments), in a READ ONLY transaction with a statement
timeout, and is always rolled back. In Mode S the harness also re-runs a failed receipt
under the service login. That can only diagnose *authorization exceeded* (the result
exists but this principal may not see it); it never upgrades a receipt to verified.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import errors

from eeb.db.load import dsn_for
from eeb.harness.dbobserve import Statement
from eeb.policy import sqlgen

MAX_ROWS = 100_000
TIMEOUT = "5s"


def _value(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v.is_finite() else str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return str(v)


Columns = Sequence[tuple[str, int]]


def result_digest(columns: Columns, rows: Iterable[Sequence[Any]], ordered: bool = False) -> str:
    rendered = [json.dumps([_value(v) for v in row], separators=(",", ":"), ensure_ascii=False)
                for row in rows]
    if not ordered:
        rendered.sort()
    doc = {"columns": [[str(n), int(t)] for n, t in columns], "ordered": bool(ordered),
           "rows": [json.loads(r) for r in rendered]}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def cursor_digest(cur: Any, rows: Sequence[Sequence[Any]], ordered: bool = False) -> str:
    """Digest for rows fetched from a psycopg cursor (what a SUT would call)."""
    cols = [(d.name, d.type_code) for d in (cur.description or [])]
    return result_digest(cols, rows, ordered)


@dataclass
class _Result:
    digest: str | None
    state: str | None
    columns: list[str]
    rows: list[dict[str, Any]]


def _execute(dsn: str, sql: str, params: Any, ordered: bool = False) -> _Result:
    """Digest (or error state) and normalized rows of a read-only, rolled-back execution."""
    with psycopg.connect(dsn) as conn:
        try:
            conn.execute("BEGIN READ ONLY")
            conn.execute(f"SET LOCAL statement_timeout = '{TIMEOUT}'")
            cur = conn.execute(sql, params)
            rows = cur.fetchmany(MAX_ROWS + 1) if cur.description else []
            if len(rows) > MAX_ROWS:
                return _Result(None, "too_large", [], [])
            cols = [d.name for d in (cur.description or [])]
            return _Result(cursor_digest(cur, rows, ordered), None, cols,
                           [dict(zip(cols, map(_value, r), strict=True)) for r in rows])
        except errors.Error as e:
            return _Result(None, e.sqlstate or type(e).__name__, [], [])
        finally:
            conn.rollback()


def _placeholders_to_dollar(sql: str) -> str:
    n = 0

    def sub(_: re.Match[str]) -> str:
        nonlocal n
        n += 1
        return f"${n}"

    return re.sub(r"%\((?:\w+)\)s|%s", sub, sql)


@dataclass
class ReceiptCheck:
    receipt_id: str
    status: str  # verified | digest_mismatch | execution_error | authorization_exceeded |
    #              invalid_receipt
    error_state: str | None
    executed_by_sut: bool
    detail: str = ""
    # The harness's own result, kept only when verified: what SQL citations are checked on.
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)


def verify_receipt(admin_dsn: str, ns: str, principal_id: str, receipt: dict[str, Any],
                   mode: str, statements: Sequence[Statement]) -> ReceiptCheck:
    rid = str(receipt.get("receipt_id", ""))
    sql, claimed = receipt.get("sql"), receipt.get("result_digest")
    if not isinstance(sql, str) or not isinstance(claimed, str):
        return ReceiptCheck(rid, "invalid_receipt", None, False, "sql and digest required")
    params = receipt.get("params")
    ordered = receipt.get("ordered", False)
    if not isinstance(ordered, bool):
        return ReceiptCheck(rid, "invalid_receipt", None, False, "ordered must be boolean")
    target = " ".join(_placeholders_to_dollar(sql).split())
    executed = any(" ".join(s.sql.split()) == target and s.outcome == "succeeded"
                   for s in statements)
    twin = dsn_for(admin_dsn, ns, sqlgen.verifier_role(ns, principal_id),
                   sqlgen.verifier_password(ns, principal_id))
    res = _execute(twin, sql, params, ordered)
    digest, state = res.digest, res.state
    if digest == claimed:
        return ReceiptCheck(rid, "verified", None, executed, columns=res.columns,
                            rows=res.rows)
    if mode == "S":
        svc = dsn_for(admin_dsn, ns, sqlgen.service_role(ns), sqlgen.service_password(ns))
        svc_digest = _execute(svc, sql, params, ordered).digest
        if svc_digest == claimed:
            return ReceiptCheck(rid, "authorization_exceeded", state, executed,
                                "result reproducible only with the service login")
    if state is not None:
        return ReceiptCheck(rid, "execution_error", state, executed)
    return ReceiptCheck(rid, "digest_mismatch", None, executed)
