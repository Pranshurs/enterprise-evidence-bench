"""Re-derive every SQL gold fact of a case corpus in Postgres, under the asker's login.

The case builder computes SQL facts in Python from the rows the policy oracle grants the
asking principal. Here each fact's ``gold_sql`` is executed on the built database through
that principal's own login, so row-level security and column grants decide what the query
sees. The two derivations must agree.

Where a case records a restricted-value probe, the same ``gold_sql`` is also run with
administrator rights, and the result must equal the recorded unauthorized value. That
proves the probe value is what an unrestricted reader would get, not a guess.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from eeb.db.load import admin, dsn_for
from eeb.policy import sqlgen


def _matches(kind: str, expected: Any, tolerance: str,
             rows: list[tuple[Any, ...]]) -> tuple[bool, Any]:
    if kind == "entity_set":
        got: Any = [r[0] for r in rows]
        return got == expected, got
    if len(rows) != 1 or len(rows[0]) != 1:
        return False, f"{len(rows)} rows"
    got = rows[0][0]
    if kind == "boolean":
        return got is expected, got
    if got is None or expected is None:
        return got is None and expected is None, got
    try:
        ok = abs(Decimal(str(got)) - Decimal(str(expected))) <= Decimal(tolerance)
    except InvalidOperation:
        ok = False
    return ok, str(got)


def check_case_gold_sql(admin_dsn: str, ns: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Run every SQL gold fact under its case's principal login; return counts and problems."""
    problems: list[dict[str, Any]] = []
    checked = probes = 0
    conns: dict[str, psycopg.Connection[Any]] = {}
    try:
        with admin(admin_dsn, ns) as root:
            for case in cases:
                facts = [f for f in case["gold_facts"] if f["source"] == "sql"]
                if not facts:
                    continue
                pid = case["principal_id"]
                if pid not in conns:
                    conns[pid] = psycopg.connect(
                        dsn_for(admin_dsn, ns, sqlgen.login_role(ns, pid),
                                sqlgen.login_password(ns, pid)), autocommit=True)
                probe = {p["fact_id"]: p for p in case.get("restricted_probe") or []}
                for f in facts:
                    checked += 1
                    where = {"case_id": case["case_id"], "principal_id": pid,
                             "fact_id": f["fact_id"]}
                    try:
                        rows = conns[pid].execute(f["gold_sql"]).fetchall()
                    except psycopg.Error as e:
                        problems.append({**where, "problem": "execution_error",
                                         "sqlstate": e.sqlstate})
                        continue
                    ok, got = _matches(f["kind"], f["value"], f["tolerance"], rows)
                    if not ok:
                        problems.append({**where, "problem": "principal_value_mismatch",
                                         "expected": f["value"], "got": got})
                    if f["fact_id"] in probe:
                        probes += 1
                        want = probe[f["fact_id"]]["unauthorized_value"]
                        ok, got = _matches(f["kind"], want, f["tolerance"],
                                           root.execute(f["gold_sql"]).fetchall())
                        if not ok:
                            problems.append({**where, "problem": "unrestricted_value_mismatch",
                                             "expected": want, "got": got})
    finally:
        for c in conns.values():
            c.close()
    return {"sql_facts_checked": checked, "restricted_probes_checked": probes,
            "principals": sorted(conns), "problems": problems}
