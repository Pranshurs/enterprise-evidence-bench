"""Canonical serialization and digests.

Every emitted artifact goes through these functions so byte-identity is a property of the
encoding, not of call sites. Rules: UTF-8, ``\\n`` line endings, sorted keys, no
insignificant whitespace, decimals and dates as strings, ``None`` as JSON null, no floats.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError("floats are not allowed in canonical artifacts")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return format(value, "f")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    raise TypeError(f"unsupported type in canonical artifact: {type(value).__name__}")


def dumps(value: Any) -> str:
    return json.dumps(
        _normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def jsonl(records: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(dumps(r) + "\n" for r in records).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def digest(value: Any) -> str:
    return sha256_text(dumps(value))
