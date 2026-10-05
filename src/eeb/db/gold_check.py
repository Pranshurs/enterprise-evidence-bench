"""Second derivation of SQL-sourced gold facts: run each ``gold_sql`` on the built database
and compare with the value the generator derived in Python from the final rows."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from eeb.db.load import admin, read_jsonl


def check_gold_sql(admin_dsn: str, instance: Path, ns: str) -> dict[str, Any]:
    facts = [f for f in read_jsonl(instance / "gold/scenario_facts.jsonl") if f["source"] == "sql"]
    mismatches: list[dict[str, Any]] = []
    with admin(admin_dsn, ns) as conn:
        for f in facts:
            rows = conn.execute(f["gold_sql"]).fetchall()
            if f["kind"] == "entity_set":
                got: Any = [r[0] for r in rows]
                ok = got == f["value"]
            elif f["kind"] == "boolean":
                got = rows[0][0]
                ok = got is f["value"]
            else:
                got = str(rows[0][0])
                ok = abs(Decimal(got) - Decimal(str(f["value"]))) <= Decimal(f["tolerance"])
            if not ok:
                mismatches.append({"fact_id": f["fact_id"], "expected": f["value"], "got": got})
    return {"sql_facts_checked": len(facts), "mismatches": mismatches}
