"""Rates as ``k/n`` with a 95% Wilson score interval (spec §10)."""

from __future__ import annotations

import math
from typing import Any

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """The Wilson score interval for ``k`` successes in ``n`` trials (None when n = 0)."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"invalid count {k}/{n}")
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def rate(k: int, n: int) -> dict[str, Any]:
    """A reported rate: the counts, the point estimate and the interval, rounded to four
    places so reports are byte-stable."""
    ci = wilson(k, n)
    return {"k": k, "n": n,
            "rate": None if n == 0 else round(k / n, 4),
            "wilson95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)]}
