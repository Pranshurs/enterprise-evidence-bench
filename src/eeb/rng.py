"""Deterministic, interpreter-independent random streams.

Python's ``random`` module is stable for most uses but its higher-level helpers have changed
between versions, and floats invite platform drift. Every draw here comes from SHA-256 in
counter mode over a key derived from ``(seed, stream name)``, and only integers leave this
module. Naming streams keeps them independent: adding draws to one stream never shifts
another.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from typing import TypeVar

T = TypeVar("T")

_U64 = 1 << 64


class Stream:
    __slots__ = ("_key", "_counter", "_buf")

    def __init__(self, seed: int, name: str) -> None:
        if seed < 0:
            raise ValueError("seed must be non-negative")
        self._key = hashlib.sha256(f"eeb-rng/v1/{seed}/{name}".encode()).digest()
        self._counter = 0
        self._buf: list[int] = []

    def child(self, name: str) -> Stream:
        """An independent stream keyed under this one."""
        s = Stream.__new__(Stream)
        s._key = hashlib.sha256(self._key + b"/" + name.encode()).digest()
        s._counter = 0
        s._buf = []
        return s

    def u64(self) -> int:
        if not self._buf:
            block = hashlib.sha256(self._key + self._counter.to_bytes(8, "big")).digest()
            self._counter += 1
            self._buf = [int.from_bytes(block[i : i + 8], "big") for i in (24, 16, 8, 0)]
        return self._buf.pop()

    def below(self, n: int) -> int:
        """Uniform integer in [0, n) by rejection sampling (no modulo bias)."""
        if n <= 0:
            raise ValueError("n must be positive")
        limit = _U64 - (_U64 % n)
        while True:
            x = self.u64()
            if x < limit:
                return x % n

    def randint(self, lo: int, hi: int) -> int:
        """Uniform integer in [lo, hi]."""
        if hi < lo:
            raise ValueError("empty range")
        return lo + self.below(hi - lo + 1)

    def chance(self, numerator: int, denominator: int) -> bool:
        return self.below(denominator) < numerator

    def choice(self, seq: Sequence[T]) -> T:
        if not seq:
            raise ValueError("empty sequence")
        return seq[self.below(len(seq))]

    def weighted(self, items: Sequence[tuple[T, int]]) -> T:
        total = sum(w for _, w in items)
        if total <= 0 or any(w < 0 for _, w in items):
            raise ValueError("weights must be non-negative with a positive sum")
        r = self.below(total)
        for item, w in items:
            if r < w:
                return item
            r -= w
        raise AssertionError("unreachable")

    def shuffled(self, seq: Sequence[T]) -> list[T]:
        out = list(seq)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def sample(self, seq: Sequence[T], k: int) -> list[T]:
        if k > len(seq):
            raise ValueError("sample larger than population")
        return self.shuffled(seq)[:k]

    def decimal(self, lo: str, hi: str, places: int) -> Decimal:
        """Uniform decimal in [lo, hi] on a grid of 10**-places."""
        scale = 10**places
        a = int(Decimal(lo) * scale)
        b = int(Decimal(hi) * scale)
        return Decimal(self.randint(a, b)).scaleb(-places)
