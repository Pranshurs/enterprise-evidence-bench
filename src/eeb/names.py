"""Fictional names, canary tokens and the well-known-name denylist (spec §4, §6.7)."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from importlib import resources

from eeb.rng import Stream

_ONSETS = ("b", "br", "d", "dr", "f", "g", "gr", "h", "k", "kr", "l", "m", "n", "p", "r",
           "s", "st", "t", "tr", "v", "z")
_NUCLEI = ("a", "e", "i", "o", "u", "ai", "ei", "ou")
_CODAS = ("", "", "n", "r", "l", "s", "nd", "rk", "lt")
_SUPPLIER_SUFFIX = {
    "CAT-MET": ("Metals", "Alloys", "Steelworks", "Foundry"),
    "CAT-ELC": ("Electronics", "Components", "Circuits", "Semitech"),
    "CAT-PKG": ("Packaging", "Cartons", "Containers", "Wraps"),
    "CAT-LOG": ("Logistics", "Freight", "Haulage", "Transit"),
    "CAT-MRO": ("Industrial Supply", "Maintenance", "Toolworks", "Spares"),
    "CAT-ITH": ("Systems", "Computing", "Devices", "Hardware"),
    None: ("Trading", "Supply", "Enterprises", "Works"),
}

CANARY_ALPHABET = "bcdfghjkmnpqrstvwxz2346789"
CANARY_RE = re.compile(r"(?<![A-Za-z0-9])qz[" + CANARY_ALPHABET + r"]{11}(?![A-Za-z0-9])")


def _syllable(s: Stream) -> str:
    return s.choice(_ONSETS) + s.choice(_NUCLEI) + s.choice(_CODAS)


def word(s: Stream, syllables: int = 2) -> str:
    return "".join(_syllable(s) for _ in range(syllables)).capitalize()


def supplier_name(s: Stream, category_id: str | None) -> str:
    return f"{word(s)} {s.choice(_SUPPLIER_SUFFIX[category_id])}"


def person_name(s: Stream) -> str:
    return f"{word(s)} {word(s, s.randint(2, 3))}"


def canary(s: Stream) -> str:
    body = "".join(s.choice(CANARY_ALPHABET) for _ in range(10))
    check = CANARY_ALPHABET[hashlib.sha256(body.encode()).digest()[0] % len(CANARY_ALPHABET)]
    return "qz" + body + check


def is_valid_canary(token: str) -> bool:
    if not CANARY_RE.fullmatch(token):
        return False
    body, check = token[2:12], token[12]
    expected = CANARY_ALPHABET[hashlib.sha256(body.encode()).digest()[0] % len(CANARY_ALPHABET)]
    return check == expected


@lru_cache(maxsize=1)
def denylist() -> frozenset[str]:
    text = resources.files("eeb").joinpath("data/denylist.txt").read_text("utf-8")
    return frozenset(
        line.strip().lower()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    )


def violates_denylist(name: str) -> bool:
    """True if any word or the whole lowercased name equals a denylisted well-known name."""
    lowered = name.lower()
    words = set(re.findall(r"[a-z0-9]+", lowered))
    deny = denylist()
    if lowered in deny:
        return True
    return any(w in deny for w in words) or any(
        " " in d and re.search(r"(?<![a-z0-9])" + re.escape(d) + r"(?![a-z0-9])", lowered)
        for d in deny
    )
