"""Value normalization shared by the scorers (spec §10.2).

A claimed value is compared with a gold value after normalization: numbers as ``Decimal``
(thousands separators, a trailing ``%`` and a leading or trailing currency code removed),
booleans from a fixed vocabulary, dates as ISO ``YYYY-MM-DD`` (ISO or ``1 July 2025``
written forms), enums and entity ids case-folded with whitespace collapsed.

Units: ``percent`` and the currency codes are the only units gold facts carry. A claimed
unit is consistent when it names the same unit; a money fact needs its currency stated,
in ``unit`` or as a code in the value or claim text.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

CURRENCIES = ("INR", "EUR", "GBP")
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}
_TRUE = {"true", "yes", "y", "met", "applies", "required", "entitled"}
_FALSE = {"false", "no", "n", "not met", "does not apply", "not required", "not entitled"}
_NUM = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_CUR = re.compile(r"\b(INR|EUR|GBP)\b", re.IGNORECASE)


def number(v: Any) -> Decimal | None:
    """The single number in ``v``, or None if there is not exactly one."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, Decimal)):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(repr(v))
    if not isinstance(v, str):
        return None
    text = _CUR.sub(" ", v).replace("%", " ")
    found = _NUM.findall(text)
    if len(found) != 1:
        return None
    try:
        return Decimal(found[0].replace(",", ""))
    except InvalidOperation:
        return None


def boolean(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = " ".join(v.lower().split())
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    return None


def date(v: Any) -> str | None:
    if isinstance(v, dt.date):
        return v.isoformat()
    if not isinstance(v, str):
        return None
    s = v.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(*map(int, m.groups())).isoformat()
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2}) ([A-Za-z]+) (\d{4})", s)
    if m and m.group(2).lower() in _MONTHS:
        try:
            return dt.date(int(m.group(3)), _MONTHS[m.group(2).lower()],
                           int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def text_key(v: Any) -> str:
    return " ".join(str(v).casefold().split())


def currency_in(*texts: Any) -> set[str]:
    out: set[str] = set()
    for t in texts:
        if isinstance(t, str):
            out |= {m.upper() for m in _CUR.findall(t)}
    return out


def unit_consistent(fact: dict[str, Any], claim: dict[str, Any]) -> bool:
    """The claim's unit does not contradict the fact's; a money fact needs its currency."""
    want = fact.get("unit")
    got = claim.get("unit")
    if fact["kind"] == "money":
        stated = currency_in(got, claim.get("value"), claim.get("text"))
        return stated == {want}
    if got is None or want is None:
        return True
    return text_key(got) in {text_key(want), "%" if want == "percent" else text_key(want)}


def matches(fact: dict[str, Any], claim: dict[str, Any]) -> bool:
    """The claim states the fact's value (§10.2): within tolerance and unit-consistent for
    numbers and money, exact after normalization otherwise."""
    kind, gold = fact["kind"], fact["value"]
    v = claim.get("value")
    if kind in ("number", "money"):
        g, n = number(gold), number(v)
        return (g is not None and n is not None and abs(n - g) <= Decimal(fact["tolerance"])
                and unit_consistent(fact, claim))
    if kind == "boolean":
        return boolean(v) is not None and boolean(v) == boolean(gold)
    if kind == "date":
        return date(v) is not None and date(v) == date(gold)
    if kind in ("entity_set", "list"):
        if not isinstance(v, list) or not isinstance(gold, list):
            return False
        return sorted(map(text_key, v)) == sorted(map(text_key, gold))
    return v is not None and text_key(v) == text_key(gold)


def comparable(fact: dict[str, Any], claim: dict[str, Any]) -> bool:
    """The claim's value is of the fact's kind (and unit), so it either states the fact or
    contradicts it."""
    kind, v = fact["kind"], claim.get("value")
    if kind in ("number", "money"):
        return number(v) is not None and unit_consistent(fact, claim)
    if kind == "boolean":
        return boolean(v) is not None
    if kind == "date":
        return date(v) is not None
    if kind in ("entity_set", "list"):
        return isinstance(v, list)
    return v is not None
