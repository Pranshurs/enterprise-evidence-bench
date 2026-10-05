"""Regression tests for Phase 2a implementation findings (see docs/FINDINGS.md)."""

from __future__ import annotations

import dataclasses

import pytest

from eeb.generator import core
from eeb.generator.core import Config
from eeb.generator.instance import build_instance_files, generate


def test_f1_many_s2_suppliers_do_not_use_a_stale_po_index(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """F-1: with more than one S2 supplier, planting the second read a PO index built before
    the first supplier's planted POs existed (KeyError). The small scale plants one S2
    supplier and never showed it; this test forces several at small-scale cost."""
    many = dataclasses.replace(core.SCALES["small"], planted_suppliers=3)
    monkeypatch.setitem(core.SCALES, "small", many)
    inst, extra = generate(Config(seed=7))
    assert len(extra["world"].s2) == 3
    files = build_instance_files(Config(seed=7))
    assert sum(1 for line in files["gold/scenario_facts.jsonl"].splitlines()
               if b'"scenario_kind":"S2"' in line) == 3 * 11
