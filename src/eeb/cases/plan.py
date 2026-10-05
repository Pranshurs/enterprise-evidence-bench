"""Case plan, split construction and the frozen corpus constraints (spec §7).

Phase 2a fixes the *shape* of the corpus: case ids, classes, counterfactual groups,
overlays and the dev/test split. Question text, principal binding and per-case gold are
bound in Phase 2b by templates. Each slot here becomes exactly one case then.

The split is group-aware (all members of a counterfactual group share a split) and
stratified per class. ``check_constraints`` proves the frozen arithmetic on the
constructed plan, and generation fails if any constraint is violated.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Any

NON_X_CLASSES = ("S", "D", "A", "U", "C", "T", "Q", "H")
SINGLE_QUOTAS = {"X": 220, "S": 45, "D": 45, "U": 45, "C": 45, "T": 45, "Q": 45, "H": 45}
COUNTERFACTUAL_GROUPS = 65  # each: one permitted principal (X) + one denied principal (A)
INJECTION_OVERLAYS = 50  # drawn from X, S, D single cases
OOL_OVERLAYS = {"X": 40, "S": 30}  # out-of-metric-layer overlays
TEST_FRACTION = (3, 5)

# Frozen minimums (spec §7, v1).
MIN_TOTAL = 560
MIN_TEST = 340
MIN_X_SHARE = (2, 5)  # 40% of every split
MIN_TEST_PER_NON_X = 25
MIN_GROUPS = 60
MIN_INJECTION = 40
MIN_OOL = 60
MIN_OOL_X = 30


def _rank(seed: int, key: str) -> str:
    return hashlib.sha256(f"eeb-split/v1/{seed}/{key}".encode()).hexdigest()


def build_plan(seed: int) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for cls, n in SINGLE_QUOTAS.items():
        for i in range(1, n + 1):
            cases.append({"case_id": f"C-{cls}-{i:04d}", "class": cls, "group_id": None,
                          "group_member": None, "overlays": {"injection": False, "ool": False}})
    for g in range(1, COUNTERFACTUAL_GROUPS + 1):
        gid = f"G-{g:04d}"
        for member, cls in (("permitted", "X"), ("denied", "A")):
            cases.append({"case_id": f"C-{gid}-{member}", "class": cls, "group_id": gid,
                          "group_member": member,
                          "overlays": {"injection": False, "ool": False}})

    # Overlays: deterministic choice by rank within eligible singles.
    singles = [c for c in cases if c["group_id"] is None]
    inj_pool = sorted((c for c in singles if c["class"] in ("X", "S", "D")),
                      key=lambda c: _rank(seed, "inj/" + c["case_id"]))
    for c in inj_pool[:INJECTION_OVERLAYS]:
        c["overlays"]["injection"] = True
    for cls, n in OOL_OVERLAYS.items():
        pool = sorted((c for c in singles if c["class"] == cls and not c["overlays"]["injection"]),
                      key=lambda c: _rank(seed, "ool/" + c["case_id"]))
        for c in pool[:n]:
            c["overlays"]["ool"] = True

    # Split: stratified per class for singles, per group for counterfactual groups.
    num, den = TEST_FRACTION
    by_class: dict[str, list[dict[str, Any]]] = {}
    for c in singles:
        by_class.setdefault(c["class"], []).append(c)
    for members in by_class.values():
        ranked = sorted(members, key=lambda c: _rank(seed, "split/" + c["case_id"]))
        k = math.ceil(len(ranked) * num / den)
        for i, c in enumerate(ranked):
            c["split"] = "test" if i < k else "dev"
    groups = sorted({c["group_id"] for c in cases if c["group_id"]},
                    key=lambda g: _rank(seed, "split/" + g))
    k = math.ceil(len(groups) * num / den)
    test_groups = set(groups[:k])
    for c in cases:
        if c["group_id"] is not None:
            c["split"] = "test" if c["group_id"] in test_groups else "dev"
    cases.sort(key=lambda c: c["case_id"])
    return cases


def check_constraints(plan: list[dict[str, Any]]) -> list[str]:
    """Return every violated frozen constraint (empty list = all hold)."""
    v: list[str] = []
    ids = [c["case_id"] for c in plan]
    if len(ids) != len(set(ids)):
        v.append("case ids are not unique")
    if len(plan) < MIN_TOTAL:
        v.append(f"total {len(plan)} < {MIN_TOTAL}")
    splits = {s: [c for c in plan if c["split"] == s] for s in ("dev", "test")}
    if any(c["split"] not in splits for c in plan):
        v.append("a case has an unknown split")
    if len(splits["test"]) < MIN_TEST:
        v.append(f"test {len(splits['test'])} < {MIN_TEST}")
    for s, members in splits.items():
        x = sum(1 for c in members if c["class"] == "X")
        if not members or x * MIN_X_SHARE[1] < len(members) * MIN_X_SHARE[0]:
            v.append(f"{s}: X share {x}/{len(members)} < 40%")
    test_counts = Counter(c["class"] for c in splits["test"])
    for cls in NON_X_CLASSES:
        if test_counts[cls] < MIN_TEST_PER_NON_X:
            v.append(f"test class {cls}: {test_counts[cls]} < {MIN_TEST_PER_NON_X}")
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in plan:
        if c["group_id"]:
            groups.setdefault(c["group_id"], []).append(c)
    if sum(1 for g in groups.values() if len(g) >= 2) < MIN_GROUPS:
        v.append(f"counterfactual groups with >=2 members < {MIN_GROUPS}")
    for gid, g in groups.items():
        if len({c["split"] for c in g}) != 1:
            v.append(f"group {gid} spans splits")
    if sum(1 for c in plan if c["overlays"]["injection"]) < MIN_INJECTION:
        v.append(f"injection overlays < {MIN_INJECTION}")
    ool = [c for c in plan if c["overlays"]["ool"]]
    if len(ool) < MIN_OOL:
        v.append(f"OOL overlays < {MIN_OOL}")
    if sum(1 for c in ool if c["class"] == "X") < MIN_OOL_X:
        v.append(f"OOL X-class overlays < {MIN_OOL_X}")
    return v


def summary(plan: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"total": len(plan)}
    for s in ("dev", "test"):
        members = [c for c in plan if c["split"] == s]
        out[s] = {"total": len(members),
                  "by_class": dict(sorted(Counter(c["class"] for c in members).items()))}
    return out
