"""Mechanical checks on paraphrased questions.

A paraphrase from an independent model family may change wording, never meaning. Meaning
cannot be checked mechanically in full, so this module checks what can be: the literal
anchors of the canonical question (identifiers, supplier names, periods, dates, currencies,
numbers) survive exactly and no new ones appear; the direction of every comparison, the
time anchor, the qualifiers that select rows, and the documents the question points to
are kept; and nothing is added that assumes wider access than the asker has.

A paraphrase that passes is *mechanically consistent* with the canonical question. The
queue records the canonical wording next to the paraphrase so that a reader can audit it.

The paraphraser receives the canonical question and its ``must_preserve`` anchors only;
gold values, evidence and restricted content never enter the queue.
"""

from __future__ import annotations

import re
from typing import Any

# Literal anchors: each occurrence must survive verbatim, and no new one may appear.
_ID = re.compile(r"\b(?:SUP-\d{4}|CTR-\d{4}|PO-\d{6}-\d{5}|INC-\d{4}|ITM-[A-Z]{3}-\d{3}"
                 r"|CC-[A-Z]{2}-\d{2})\b")
_QUARTER = re.compile(r"\b\d{4}Q[1-4]\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_CURRENCY = re.compile(r"\b(?:INR|EUR|GBP)\b")
_SUPPLIER = re.compile(r"((?:[A-Z][A-Za-z&.'-]*\s){1,3}[A-Z][A-Za-z&.'-]*) \((SUP-\d{4})\)")
_NUMBER = re.compile(r"(?<![\w-])\d+(?:\.\d+)?(?![\w-])")

# Meaning-bearing phrases, as groups of interchangeable wordings. A group present in the
# canonical question must be present in the paraphrase, and a group absent from the
# canonical question must stay absent. Groups listed together under one key are opposites.
_GROUPS: dict[str, tuple[str, ...]] = {
    "above": (r"\babove\b", r"\bexceed", r"\bmore than\b", r"\bover\b", r"\bhigher than\b",
              r"\bgreater than\b"),
    "below": (r"\bbelow\b", r"\bfall short\b", r"\bfell short\b", r"\bless than\b",
              r"\bunder\b(?! contract| its| the| a\b)", r"\blower than\b"),
    "within": (r"\bwithin\b", r"\bno more than\b", r"\bat most\b", r"\bnot exceed"),
    "late": (r"\blate\b", r"\bafter (?:their|its|the) promised date\b", r"\boverdue\b",
             r"\bdelayed\b"),
    "on_time": (r"\bon time\b", r"\bon-time\b"),
    "today": (r"\btoday\b", r"\bcurrently\b", r"\bas of now\b", r"\bat present\b",
              r"\bright now\b"),
    "at_the_time": (r"\bat the time\b", r"\bin force on that date\b", r"\bthen in force\b",
                    r"\bin force at the time\b", r"\bduring\b", r"\bon that date\b"),
    "off_contract": (r"\boff-contract\b", r"\boff contract\b", r"\bnot covered by a contract\b"),
    "unpaid": (r"\bnot been paid\b", r"\bunpaid\b", r"\boutstanding\b", r"\bnot yet paid\b"),
    "before_exclusions": (r"\bbefore (?:any )?(?:contractual )?exclusions\b", r"\braw\b",
                          r"\bunadjusted\b"),
    "force_majeure": (r"\bforce majeure\b",),
    "caused_by_buyer": (r"\bcaused by ExampleCo\b", r"\bExampleCo'?s? fault\b",
                        r"\battributable to ExampleCo\b"),
    "exception": (r"\bCFO exception\b",),
    "rebate": (r"\brebate\b",),
    "sla": (r"\bservice level schedule\b", r"\bservice level agreement\b", r"\bSLA\b"),
    "policy": (r"\bprocurement policy\b",),
    "signed": (r"\bsigned\b",),
    "contract": (r"(?<!off-)(?<!off )\bcontract\b", r"\bagreement\b"),
    "percent_points": (r"\bpercentage points?\b", r"\bpoints\b"),
    "percent": (r"\bper ?cent\b", r"%", r"\bshare\b", r"\brate\b", r"\bproportion\b"),
    "how_many": (r"\bhow many\b", r"\bnumber of\b", r"\bcount\b"),
    "total": (r"\btotal\b", r"\bsum\b", r"\baltogether\b", r"\bin all\b",
              r"\bcombined\b", r"\bhow much\b"),
    "average": (r"\baverage\b", r"\bmean\b"),
}
# Opposite directions: a paraphrase may not introduce the opposite of a direction the
# canonical question uses.
_OPPOSITES = (("above", "below"), ("late", "on_time"), ("today", "at_the_time"))

# Wording that assumes access the asker may not have. Never allowed in a paraphrase
# unless the canonical question already says it.
_WIDER_ACCESS = re.compile(
    r"\b(?:admin(?:istrator)?|superuser|root|all business units|every business unit|"
    r"across (?:all|every) (?:units|business units|teams)|company-wide|regardless of "
    r"(?:access|permissions?)|ignore|ignoring|bypass|override|unrestricted|full access|"
    r"everyone'?s|confidential (?:data|records)|restricted)\b", re.IGNORECASE)


def _groups(text: str) -> set[str]:
    return {g for g, pats in _GROUPS.items()
            if any(re.search(p, text, re.IGNORECASE) for p in pats)}


def must_preserve(question: str) -> dict[str, list[str]]:
    """The literal anchors of ``question`` a paraphrase must keep verbatim."""
    suppliers = sorted({f"{m.group(1)} ({m.group(2)})" for m in _SUPPLIER.finditer(question)})
    without_anchors = _DATE.sub(" ", _QUARTER.sub(" ", _ID.sub(" ", question)))
    return {
        "identifiers": sorted(set(_ID.findall(question))),
        "suppliers": suppliers,
        "periods": sorted(set(_QUARTER.findall(question))),
        "dates": sorted(set(_DATE.findall(question))),
        "currencies": sorted(set(_CURRENCY.findall(question))),
        "numbers": sorted(set(_NUMBER.findall(without_anchors))),
        "meaning": sorted(_groups(question)),
    }


def check(canonical: str, paraphrase: str | None, model_family: str | None,
          reference_family: str | None = None) -> list[str]:
    """Problems with ``paraphrase`` of ``canonical`` (empty: mechanically consistent)."""
    if not paraphrase or not paraphrase.strip():
        return ["no paraphrase"]
    out: list[str] = []
    if not model_family or not model_family.strip():
        out.append("paraphrasing model family not recorded")
    elif reference_family and model_family.strip().lower() == reference_family.strip().lower():
        out.append(f"paraphrased by {model_family}, the reference agent's model family")
    if " ".join(paraphrase.split()).lower() == " ".join(canonical.split()).lower():
        out.append("paraphrase is the canonical wording")
    want, got = must_preserve(canonical), must_preserve(paraphrase)
    for kind, label in (("identifiers", "identifier"), ("periods", "period"),
                        ("dates", "date"), ("currencies", "currency"), ("numbers", "number")):
        for v in want[kind]:
            if v not in got[kind]:
                out.append(f"{label} {v} missing")
        for v in got[kind]:
            if v not in want[kind]:
                out.append(f"{label} {v} added")
    for name in want["suppliers"]:
        if name.split(" (")[0] not in paraphrase:
            out.append(f"supplier name {name.split(' (')[0]!r} not kept verbatim")
    have, need = set(got["meaning"]), set(want["meaning"])
    for g in sorted(need - have):
        out.append(f"meaning '{g}' lost")
    for g in sorted(have - need):
        out.append(f"meaning '{g}' added")
    for a, b in _OPPOSITES:
        if (a in need and b in have and b not in need) or (b in need and a in have
                                                          and a not in need):
            out.append(f"direction reversed between '{a}' and '{b}'")
    added = {m.group(0).lower() for m in _WIDER_ACCESS.finditer(paraphrase)} - {
        m.group(0).lower() for m in _WIDER_ACCESS.finditer(canonical)}
    for w in sorted(added):
        out.append(f"assumes wider access: {w!r}")
    return out


def check_queue(entries: list[dict[str, Any]], reference_family: str | None = None
                ) -> dict[str, Any]:
    """Check every entry of a rephrase queue. Entries with no paraphrase are pending."""
    results = []
    for e in entries:
        if not e.get("rephrased_question"):
            results.append({"family_id": e["family_id"], "status": "PENDING_REPHRASE",
                            "problems": []})
            continue
        probs = check(e["question_canonical"], e["rephrased_question"],
                      e.get("rephrased_by_model_family"), reference_family)
        results.append({"family_id": e["family_id"],
                        "status": "rejected" if probs else "mechanically_consistent",
                        "problems": probs})
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"counts": dict(sorted(counts.items())), "entries": results,
            "passed": all(r["status"] == "mechanically_consistent" for r in results)}
