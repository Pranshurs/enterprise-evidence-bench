"""A gold-perfect response for a case: the clean arm of every scorer (spec §15 G6).

Built from the case's gold only. Each answer-requirement fact becomes one claim carrying a
citation of every kind the case requires for it: document spans of the doc facts it rests
on, and one SQL receipt per SQL fact it rests on (the gold SQL; its re-executed rows come
from the harness, not from here). Expected conflicts are disclosed with both sides cited;
abstentions use one fixed template; clarifications list every legal resolution.
"""

from __future__ import annotations

from typing import Any

ABSTAIN_TEXT = "I can't answer that from the sources available to you."


def _closure(facts: dict[str, dict[str, Any]], fid: str) -> list[dict[str, Any]]:
    out, stack, seen = [], [fid], set()
    while stack:
        f = facts[stack.pop()]
        if f["fact_id"] in seen:
            continue
        seen.add(f["fact_id"])
        out.append(f)
        if f["source"] == "derived":
            stack.extend(f["derived"]["inputs"])
        stack.extend(f.get("depends_on", []))
    return out


def quarters(lo: str, hi: str) -> list[str]:
    out, (y, q) = [], (int(lo[:4]), int(lo[5]))
    while f"{y}Q{q}" <= hi:
        out.append(f"{y}Q{q}")
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def receipt_id(fact_id: str) -> str:
    return f"R-{fact_id}"


def value_text(f: dict[str, Any]) -> tuple[Any, str | None]:
    """The claim value and unit for a gold fact."""
    v = f["value"]
    if f["kind"] == "money":
        return f"{f['unit']} {v}", f["unit"]
    if f["kind"] == "boolean":
        return ("true" if v else "false"), None
    return v, f.get("unit")


def citation_for(f: dict[str, Any]) -> dict[str, Any]:
    if f["source"] == "doc":
        r = f["doc_ref"]
        return {"kind": "doc", "doc_id": r["doc_id"], "version": r["version"],
                "start": r["start"], "end": r["end"]}
    return {"kind": "sql", "receipt_id": receipt_id(f["fact_id"]), "rows": [0],
            "columns": ["value"]}


def _citations(facts: dict[str, dict[str, Any]], fid: str, kinds: list[str]
               ) -> list[dict[str, Any]]:
    leaves = [f for f in _closure(facts, fid) if f["source"] in kinds]
    return [citation_for(f) for f in leaves]


def ideal_response(case: dict[str, Any]) -> dict[str, Any]:
    facts = {f["fact_id"]: f for f in case["gold_facts"]}
    out: dict[str, Any] = {"outcome": case["expected_outcome"], "answer_text": "",
                           "claims": [], "conflicts": [], "clarify": None,
                           "sql_receipts": [], "user_trace": {}}
    if case["expected_outcome"] == "ABSTAIN":
        out["answer_text"] = ABSTAIN_TEXT
        return out
    if case["expected_outcome"] == "CLARIFY":
        res = case["clarify"]["resolutions"]
        opts = (quarters(res["from"], res["to"]) if res.get("kind") == "quarter_range"
                else list(res.get("values", [])))
        out["clarify"] = {"axis": case["clarify"]["axis"], "options": opts}
        out["answer_text"] = "Which do you mean: " + ", ".join(out["clarify"]["options"]) + "?"
        return out
    kinds = {rc["fact_id"]: rc["kinds"] for rc in case["required_citations"]}
    used_sql: dict[str, dict[str, Any]] = {}
    for fid in case["answer_requirement"]:
        f = facts[fid]
        value, unit = value_text(f)
        cits = _citations(facts, fid, kinds.get(fid, [f["source"]]
                                                  if f["source"] != "derived" else ["doc", "sql"]))
        for c in cits:
            if c["kind"] == "sql":
                used_sql[c["receipt_id"]] = facts[c["receipt_id"][2:]]
        out["claims"].append({"text": f"{fid}: {value}" + (f" {unit}" if unit and
                                                           f["kind"] != "money" else ""),
                              "value": value, "unit": unit, "citations": cits})
    for ec in case["expected_conflicts"]:
        a, b = (facts[x] for x in ec["facts"])
        for f in (a, b):
            if f["source"] == "sql":
                used_sql[receipt_id(f["fact_id"])] = f
        out["conflicts"].append({"evidence": [citation_for(a), citation_for(b)],
                                 "note": ec["resolution"]})
    out["sql_receipts"] = [{"receipt_id": rid, "sql": f["gold_sql"], "params": None,
                            "db_login": None, "rowcount": 1, "result_digest": "",
                            "rows": [{"value": f["value"]}]}
                           for rid, f in sorted(used_sql.items())]
    out["answer_text"] = "; ".join(c["text"] for c in out["claims"]) + "."
    return out


def sql_facts_cited(resp: dict[str, Any], case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Receipt id -> gold SQL fact, for the receipts an ideal response cites."""
    facts = {f["fact_id"]: f for f in case["gold_facts"]}
    return {r["receipt_id"]: facts[r["receipt_id"][2:]] for r in resp.get("sql_receipts", [])}
