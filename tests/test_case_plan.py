"""The split construction proves the frozen §7 arithmetic; each constraint can fail."""

from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any

import pytest

from eeb.cases.plan import NON_X_CLASSES, build_plan, check_constraints


@pytest.mark.parametrize("seed", [0, 1, 7, 42, 2026, 99991])
def test_frozen_constraints_hold(seed: int) -> None:
    plan = build_plan(seed)
    assert check_constraints(plan) == []
    test = [c for c in plan if c["split"] == "test"]
    assert len(plan) >= 560 and len(test) >= 340
    for split in ("dev", "test"):
        members = [c for c in plan if c["split"] == split]
        assert 5 * sum(c["class"] == "X" for c in members) >= 2 * len(members)
    counts = Counter(c["class"] for c in test)
    assert all(counts[k] >= 25 for k in NON_X_CLASSES) and len(NON_X_CLASSES) == 8


def test_plan_is_deterministic_and_seed_dependent() -> None:
    assert json.dumps(build_plan(5)) == json.dumps(build_plan(5))
    assert [c["split"] for c in build_plan(5)] != [c["split"] for c in build_plan(6)]


def test_case_ids_are_canonical() -> None:
    ids = [c["case_id"] for c in build_plan(3)]
    assert ids == sorted(ids) and all(i.startswith("C-") for i in ids)
    assert ids == [c["case_id"] for c in build_plan(4)]  # ids never depend on the seed


def _move(plan: list[dict[str, Any]], pred: Any, n: int, to: str) -> None:
    moved = 0
    for c in plan:
        if moved < n and pred(c) and c["split"] != to:
            c["split"] = to
            moved += 1


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p.__delitem__(slice(0, 120)), "total"),
    (lambda p: _move(p, lambda c: c["group_id"] is None, 80, "dev"), "test "),
    (lambda p: _move(p, lambda c: c["class"] == "X" and c["group_id"] is None, 90, "dev"),
     "test: X share"),
    (lambda p: _move(p, lambda c: c["class"] == "X" and c["group_id"] is None, 90, "test"),
     "dev: X share"),
    (lambda p: _move(p, lambda c: c["class"] == "Q", 5, "dev"), "test class Q"),
    (lambda p: _move(p, lambda c: c["group_member"] == "denied", 1, "dev"), "spans splits"),
    (lambda p: [c["overlays"].__setitem__("injection", False) for c in p], "injection"),
    (lambda p: [c["overlays"].__setitem__("ool", c["class"] != "X" and c["overlays"]["ool"])
                for c in p], "OOL X-class"),
    (lambda p: p.append(copy.deepcopy(p[0])), "not unique"),
])
def test_each_constraint_can_fail(mutate: Any, needle: str) -> None:
    plan = build_plan(7)
    mutate(plan)
    violations = check_constraints(plan)
    assert any(needle in v for v in violations), violations


def test_constraints_hold_on_the_generated_plan_file(files: dict[str, bytes]) -> None:
    """Derived from the emitted artifact, not from a recomputed plan."""
    plan = [json.loads(x) for x in files["cases/plan.jsonl"].decode().splitlines()]
    assert check_constraints(plan) == []
    test = [c for c in plan if c["split"] == "test"]
    dev = [c for c in plan if c["split"] == "dev"]
    assert len(plan) == len(test) + len(dev) >= 560 and len(test) >= 340
    for members in (dev, test):
        assert 5 * sum(c["class"] == "X" for c in members) >= 2 * len(members)
    assert min(Counter(c["class"] for c in test)[k] for k in NON_X_CLASSES) >= 25
    meta = json.loads(files["INSTANCE.json"])
    assert meta["case_plan"]["test"]["total"] == len(test)
