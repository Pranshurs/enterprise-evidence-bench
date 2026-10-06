"""The SUT response contract (spec §8) and its validation.

A response that breaks the contract is not repaired: it is scored as ``INVALID``, which
counts as neither an answer nor an abstention, and the problems are reported.
"""

from __future__ import annotations

import json
from typing import Any

OUTCOMES = ("ANSWER", "ABSTAIN", "CLARIFY")
INVALID = "INVALID"


def _citation_problems(c: Any, where: str) -> list[str]:
    if not isinstance(c, dict):
        return [f"{where}: citation is not an object"]
    if c.get("kind") == "doc":
        out = [f"{where}: doc citation lacks {k}" for k in ("doc_id", "version", "start", "end")
               if k not in c]
        if not out and not all(isinstance(c[k], int) and not isinstance(c[k], bool)
                               for k in ("version", "start", "end")):
            out.append(f"{where}: doc citation version/start/end must be integers")
        return out
    if c.get("kind") == "sql":
        out = [f"{where}: sql citation lacks {k}" for k in ("receipt_id", "rows", "columns")
               if k not in c]
        if not out and not (isinstance(c["rows"], list)
                            and all(isinstance(r, int) and not isinstance(r, bool)
                                    for r in c["rows"])
                            and isinstance(c["columns"], list)
                            and all(isinstance(x, str) for x in c["columns"])):
            out.append(f"{where}: sql citation rows must be integers and columns strings")
        return out
    return [f"{where}: citation kind must be doc or sql"]


def problems(r: Any) -> list[str]:
    """Contract violations of a response (empty: valid)."""
    if not isinstance(r, dict):
        return ["response is not an object"]
    out: list[str] = []
    if r.get("outcome") not in OUTCOMES:
        out.append(f"outcome must be one of {', '.join(OUTCOMES)}")
    if not isinstance(r.get("answer_text", ""), str):
        out.append("answer_text must be a string")
    claims = r.get("claims", [])
    if not isinstance(claims, list):
        out.append("claims must be a list")
        claims = []
    for i, cl in enumerate(claims):
        if not isinstance(cl, dict) or not isinstance(cl.get("text"), str):
            out.append(f"claims[{i}]: needs a text")
            continue
        cits = cl.get("citations", [])
        if not isinstance(cits, list):
            out.append(f"claims[{i}]: citations must be a list")
            continue
        for j, c in enumerate(cits):
            out += _citation_problems(c, f"claims[{i}].citations[{j}]")
    conflicts = r.get("conflicts", [])
    if not isinstance(conflicts, list):
        out.append("conflicts must be a list")
        conflicts = []
    for i, cf in enumerate(conflicts):
        ev = cf.get("evidence") if isinstance(cf, dict) else None
        if not isinstance(ev, list) or len(ev) != 2:
            out.append(f"conflicts[{i}]: evidence must be two citations")
            continue
        for j, c in enumerate(ev):
            out += _citation_problems(c, f"conflicts[{i}].evidence[{j}]")
    cq = r.get("clarify")
    if cq is not None and not (isinstance(cq, dict) and isinstance(cq.get("axis"), str)
                               and isinstance(cq.get("options"), list)):
        out.append("clarify must be null or {axis, options}")
    if r.get("outcome") == "CLARIFY" and cq is None:
        out.append("a CLARIFY outcome needs clarify")
    receipts = r.get("sql_receipts", [])
    if not isinstance(receipts, list):
        out.append("sql_receipts must be a list")
        receipts = []
    ids = [x.get("receipt_id") for x in receipts if isinstance(x, dict)]
    if len(ids) != len(receipts) or any(not isinstance(i, str) for i in ids):
        out.append("every sql receipt needs a string receipt_id")
    elif len(set(ids)) != len(ids):
        out.append("sql receipt ids repeat")
    return out


def outcome(r: Any) -> str:
    """The scored outcome: the response's own, or INVALID."""
    return INVALID if problems(r) else str(r["outcome"])


def text_fields(r: dict[str, Any]) -> dict[str, str]:
    """User-visible text of a response, by field, for exposure scanning (§10.3)."""
    return {
        "answer_text": str(r.get("answer_text", "")),
        "claims": json.dumps(r.get("claims", []), sort_keys=True, default=str),
        "conflicts": json.dumps(r.get("conflicts", []), sort_keys=True, default=str),
        "clarify": json.dumps(r.get("clarify"), sort_keys=True, default=str),
        "user_trace": json.dumps(r.get("user_trace"), sort_keys=True, default=str),
    }
