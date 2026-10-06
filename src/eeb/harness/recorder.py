"""Append-only, hash-chained gateway log (ADR-0005 §3).

Each record's hash covers its canonical content together with the previous record's hash,
so deletion, reordering or edits are detectable by ``verify_chain``. The log belongs to the
harness; the system under test never has write access to it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class Recorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS
        if self.path.exists():
            raise FileExistsError(f"{path} exists; gateway logs are never appended across runs")
        self.path.touch()

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            body = {**record, "seq": self._seq, "prev_hash": self._prev}
            h = hashlib.sha256(_canonical(body)).hexdigest()
            full = {**body, "hash": h}
            with self.path.open("ab") as fh:
                fh.write(_canonical(full) + b"\n")
            self._seq += 1
            self._prev = h
            return full


def read_log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line]


def verify_chain(records: list[dict[str, Any]]) -> list[str]:
    """Return integrity problems (empty list = intact chain)."""
    problems: list[str] = []
    prev = GENESIS
    for i, rec in enumerate(records):
        if rec.get("seq") != i:
            problems.append(f"record {i}: seq {rec.get('seq')}")
        if rec.get("prev_hash") != prev:
            problems.append(f"record {i}: broken link")
        body = {k: v for k, v in rec.items() if k != "hash"}
        if hashlib.sha256(_canonical(body)).hexdigest() != rec.get("hash"):
            problems.append(f"record {i}: hash mismatch")
        prev = rec.get("hash", "")
    return problems
