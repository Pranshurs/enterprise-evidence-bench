"""SQL receipt canonicalization and re-execution (spec §8, §9.3).

Canonical result digest (published, so any SUT can compute it):
1. Normalize each value: NULL → null; booleans and integers as JSON; decimals as a
   normalized decimal string (``Decimal.normalize``, fixed-point); floats via ``repr``;
   dates and times as ISO-8601; bytes as hex; everything else as ``str``.
2. Render each row as compact JSON (sorted keys do not apply; rows are lists).
3. Sort the rows by that rendering.
4. SHA-256 the compact JSON of the sorted list.

Column names are not part of the digest, so aliasing does not change it.

Re-execution runs under the asking principal's **verifier twin** (harness-only
credentials, same grants and assignments), in a READ ONLY transaction with a statement
timeout, and is always rolled back. In Mode S the harness also re-runs a failed receipt
under the service login, to tell *authorization exceeded* apart from *fabricated*.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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


def result_digest(rows: Iterable[Sequence[Any]]) -> str:
    rendered = sorted(json.dumps([_value(v) for v in row], separators=(",", ":"),
                                 ensure_ascii=False) for row in rows)
    return hashlib.sha256(("[" + ",".join(rendered) + "]").encode()).hexdigest()


def _execute(dsn: str, sql: str, params: Any) -> tuple[str | None, str | None]:
    """(digest, error_state) for a read-only, rolled-back execution."""
    with psycopg.connect(dsn) as conn:
        try:
            conn.execute("BEGIN READ ONLY")
            conn.execute(f"SET LOCAL statement_timeout = '{TIMEOUT}'")
            cur = conn.execute(sql, params)
            rows = cur.fetchmany(MAX_ROWS + 1) if cur.description else []
            if len(rows) > MAX_ROWS:
                return None, "too_large"
            return result_digest(rows), None
        except errors.Error as e:
            return None, e.sqlstate or type(e).__name__
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


def verify_receipt(admin_dsn: str, ns: str, principal_id: str, receipt: dict[str, Any],
                   mode: str, statements: Sequence[Statement]) -> ReceiptCheck:
    rid = str(receipt.get("receipt_id", ""))
    sql, claimed = receipt.get("sql"), receipt.get("result_digest")
    if not isinstance(sql, str) or not isinstance(claimed, str):
        return ReceiptCheck(rid, "invalid_receipt", None, False, "sql and digest required")
    params = receipt.get("params")
    target = " ".join(_placeholders_to_dollar(sql).split())
    executed = any(" ".join(s.sql.split()) == target and s.outcome == "succeeded"
                   for s in statements)
    twin = dsn_for(admin_dsn, ns, sqlgen.verifier_role(ns, principal_id),
                   sqlgen.verifier_password(ns, principal_id))
    digest, state = _execute(twin, sql, params)
    if digest == claimed:
        return ReceiptCheck(rid, "verified", None, executed)
    if mode == "S":
        svc = dsn_for(admin_dsn, ns, sqlgen.service_role(ns), sqlgen.service_password(ns))
        svc_digest, _ = _execute(svc, sql, params)
        if svc_digest == claimed:
            return ReceiptCheck(rid, "authorization_exceeded", state, executed,
                                "result reproducible only with the service login")
    if state is not None:
        return ReceiptCheck(rid, "execution_error", state, executed)
    return ReceiptCheck(rid, "digest_mismatch", None, executed)
