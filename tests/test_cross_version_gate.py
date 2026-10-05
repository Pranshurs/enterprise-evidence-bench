"""Negative control for the cross-interpreter byte-identity gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from eeb import canonical
from eeb.generator.core import Config
from eeb.generator.instance import build_instance_files

_spec = importlib.util.spec_from_file_location(
    "cvd", Path(__file__).resolve().parents[1] / "scripts" / "cross_version_digests.py")
assert _spec and _spec.loader
cvd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cvd)


def _run(files: dict[str, bytes], label: str) -> dict[str, object]:
    return {"python": label, "files": {k: canonical.sha256_bytes(v) for k, v in files.items()}}


def test_gate_flags_a_single_changed_byte(files: dict[str, bytes]) -> None:
    tampered = dict(files)
    tampered["tables/suppliers.jsonl"] = files["tables/suppliers.jsonl"] + b" "
    diffs = cvd.differing_files([_run(files, "a"), _run(tampered, "b")])
    assert diffs == {"b": ["tables/suppliers.jsonl"]}


def test_gate_flags_a_different_generation(files: dict[str, bytes]) -> None:
    other = build_instance_files(Config(seed=8))
    assert cvd.differing_files([_run(files, "a"), _run(other, "b")])["b"]


def test_gate_passes_identical_runs(files: dict[str, bytes]) -> None:
    assert cvd.differing_files([_run(files, "a"), _run(dict(files), "b")]) == {"b": []}
