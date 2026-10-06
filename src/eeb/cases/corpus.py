"""Assemble the case corpus from an instance: cases, review set, rephrase queue, manifest.

Everything here is a pure function of the instance directory and the code. There are no
timestamps, host names or paths in any output, so two builds of the same instance are
byte-identical and the manifest can bind them.

The corpus is **not frozen** by this module. Two human-in-the-loop steps stay open and the
manifest says so:

- the stratified review set carries empty ``reviewed`` / ``issue`` / ``resolution`` fields
  for manual validation;
- at least a fifth of the test cases are marked ``PENDING_REPHRASE``. Their question text
  is still the template rendering until a paraphrase from an independent model family is
  supplied and validated.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from eeb import canonical
from eeb.cases.build import CaseBuilder
from eeb.cases.view import InstanceData
from eeb.db.load import read_jsonl
from eeb.facts_version import CASE_SCHEMA_VERSION, VALIDATOR_VERSION
from eeb.metrics import layer

REVIEW_TARGET = 60
REPHRASE_FRACTION = (1, 5)  # of the test cases of every class
PENDING_REPHRASE = "PENDING_REPHRASE"
STATUS_UNFROZEN = "UNFROZEN"
# Code whose behaviour decides the corpus; its bytes are bound into the manifest.
SOURCES = ("cases/build.py", "cases/corpus.py", "cases/facts.py", "cases/templates.py",
           "cases/validate.py", "cases/view.py", "metrics/reference.py", "data/metrics.yaml")


def _rank(seed: int, purpose: str, key: str) -> str:
    return hashlib.sha256(f"eeb-cases/v1/{seed}/{purpose}/{key}".encode()).hexdigest()


# ---------------------------------------------------------------------------- rephrasing
def mark_rephrase(cases: list[dict[str, Any]], seed: int) -> list[str]:
    """Mark at least ``REPHRASE_FRACTION`` of each class's test cases ``PENDING_REPHRASE``.

    Both members of a counterfactual group ask the same question, so they are marked
    together. Returns the marked case ids."""
    num, den = REPHRASE_FRACTION
    by_group: dict[str, list[dict[str, Any]]] = {}
    for c in cases:
        if c["group_id"]:
            by_group.setdefault(c["group_id"], []).append(c)
    marked: set[str] = set()
    for cls in sorted({c["class"] for c in cases}):
        members = sorted((c for c in cases if c["class"] == cls and c["split"] == "test"),
                         key=lambda c: _rank(seed, "rephrase", c["case_id"]))
        need = math.ceil(len(members) * num / den)
        for c in members:
            if sum(1 for m in members if m["case_id"] in marked) >= need:
                break
            marked.add(c["case_id"])
            for partner in by_group.get(c["group_id"] or "", []):
                marked.add(partner["case_id"])
    for c in cases:
        if c["case_id"] in marked:
            c["rephrase_status"] = PENDING_REPHRASE
    return sorted(marked)


def rephrase_queue(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per question to paraphrase (group members share an entry)."""
    out: dict[str, dict[str, Any]] = {}
    for c in cases:
        if c["rephrase_status"] != PENDING_REPHRASE:
            continue
        e = out.setdefault(c["family_id"], {
            "family_id": c["family_id"], "case_ids": [], "class": c["class"],
            "template_id": c["template_id"], "question_canonical": c["question_canonical"],
            "rephrased_question": None, "rephrased_by_model_family": None,
            "meaning_preserved_check": None})
        e["case_ids"].append(c["case_id"])
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------------------- review set
def select_review_set(cases: list[dict[str, Any]], seed: int) -> list[str]:
    """About ``REVIEW_TARGET`` case ids covering every template, class, split and overlay,
    every kind of outcome, and whole counterfactual groups."""
    ranked = sorted(cases, key=lambda c: _rank(seed, "review", c["case_id"]))
    by_id = {c["case_id"]: c for c in cases}
    chosen: dict[str, None] = {}

    def take(pred: Any, n: int = 1) -> None:
        got = 0
        for c in ranked:
            if got >= n:
                return
            if c["case_id"] not in chosen and pred(c):
                chosen[c["case_id"]] = None
                got += 1

    for tid in sorted({c["template_id"] for c in cases}):
        take(lambda c, tid=tid: c["template_id"] == tid)
    for cls in sorted({c["class"] for c in cases}):
        for split in ("dev", "test"):
            take(lambda c, cls=cls, split=split: c["class"] == cls and c["split"] == split)
    take(lambda c: c["overlays"]["injection"], 3)
    take(lambda c: c["overlays"]["ool"] and c["class"] == "X", 3)
    take(lambda c: c["overlays"]["ool"] and c["class"] == "S", 3)
    take(lambda c: c["restricted_probe"], 3)
    take(lambda c: c["group_member"] == "permitted", 3)
    classes = sorted({c["class"] for c in cases})
    while len(chosen) < REVIEW_TARGET and len(chosen) < len(cases):
        before = len(chosen)
        for cls in classes:
            if len(chosen) < REVIEW_TARGET:
                take(lambda c, cls=cls: c["class"] == cls)
        if len(chosen) == before:
            break
    # A counterfactual pair is reviewed as a pair.
    for cid in list(chosen):
        gid = by_id[cid]["group_id"]
        if gid:
            for c in cases:
                if c["group_id"] == gid:
                    chosen[c["case_id"]] = None
    return sorted(chosen)


def _fact_for_review(f: dict[str, Any]) -> dict[str, Any]:
    out = {"fact_id": f["fact_id"], "source": f["source"], "value": f["value"],
           "unit": f["unit"]}
    if f["source"] == "sql":
        out["gold_sql"] = f["gold_sql"]
    elif f["source"] == "doc":
        out["document"] = {k: f["doc_ref"][k] for k in ("doc_id", "version", "phrase")}
    else:
        out["derived"] = f["derived"]
    return out


def review_record(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": c["case_id"], "class": c["class"], "split": c["split"],
        "template_id": c["template_id"], "family_id": c["family_id"],
        "group_id": c["group_id"], "group_member": c["group_member"],
        "overlays": c["overlays"], "principal_id": c["principal_id"], "as_of": c["as_of"],
        "question": c["question"], "expected_outcome": c["expected_outcome"],
        "abstention_condition": c["abstention_condition"],
        "answer_requirement": c["answer_requirement"],
        "facts": [_fact_for_review(f) for f in c["gold_facts"]],
        "expected_conflicts": c["expected_conflicts"], "clarify": c["clarify"],
        "temporal": c["temporal"], "unanswerable_proof": c["unanswerable_proof"],
        "denied_evidence": c.get("denied_evidence"), "injection": c["injection"],
        "restricted_probe": c["restricted_probe"],
        # The reviewer fills these three fields and nothing else.
        "review": {"reviewed": None, "issue": None, "resolution": None},
    }


# ---------------------------------------------------------------------------- reporting
def _count(cases: list[dict[str, Any]], key: Any) -> dict[str, int]:
    return dict(sorted(Counter(str(key(c)) for c in cases).items()))


def build_report(cases: list[dict[str, Any]], rejections: Counter[str]) -> dict[str, Any]:
    singles = [c for c in cases if not c["group_id"]]
    x = [c for c in cases if c["class"] == "X" and c["necessity"]]
    return {
        "total": len(cases),
        "by_split": _count(cases, lambda c: c["split"]),
        "by_class": _count(cases, lambda c: c["class"]),
        "by_template": _count(cases, lambda c: c["template_id"]),
        "by_expected_outcome": _count(cases, lambda c: c["expected_outcome"]),
        "by_principal": _count(cases, lambda c: c["principal_id"]),
        "families": len({c["family_id"] for c in cases}),
        "families_used_by_more_than_one_single_case": sum(
            1 for n in Counter(c["family_id"] for c in singles).values() if n > 1),
        "counterfactual_groups": len({c["group_id"] for c in cases if c["group_id"]}),
        "injection_overlays": sum(1 for c in cases if c["overlays"]["injection"]),
        "out_of_layer": _count([c for c in cases if c["overlays"]["ool"]], lambda c: c["class"]),
        "cross_source_necessity": {
            "x_cases": len(x),
            "sql_alone_sufficient": sum(1 for c in x if c["necessity"]["sql_alone_sufficient"]),
            "docs_alone_sufficient": sum(1 for c in x
                                         if c["necessity"]["docs_alone_sufficient"])},
        "metric_layer": _count([c for c in cases if c["in_metric_layer"] is not None],
                               lambda c: f"{c['class']}/designated_ool={c['overlays']['ool']}"
                                         f"/in_layer={c['in_metric_layer']}"),
        "restricted_probe_cases": sum(1 for c in cases if c["restricted_probe"]),
        "pending_rephrase": sum(1 for c in cases if c["rephrase_status"] == PENDING_REPHRASE),
        "rejected_candidates": dict(sorted(rejections.items())),
    }


# ---------------------------------------------------------------------------- assembly
def source_digests() -> dict[str, str]:
    root = resources.files("eeb")
    return {s: canonical.sha256_bytes(root.joinpath(s).read_bytes()) for s in SOURCES}


GoldCheck = Callable[[list[dict[str, Any]]], dict[str, Any]]


def assemble(instance: Path, gold_sql_check: dict[str, Any] | GoldCheck | None = None,
             plan: list[dict[str, Any]] | None = None) -> dict[str, bytes]:
    """Build every corpus file for ``instance``. Returns ``{file name: bytes}``.

    ``gold_sql_check`` is either a recorded result or a function that checks the built
    cases and returns one. ``plan`` replaces the instance's frozen plan; only tests on
    fixtures too small for the full plan pass it."""
    data = InstanceData.load(instance)
    seed = int(data.meta["config"]["seed"])
    plan_bytes = (instance / "cases/plan.jsonl").read_bytes()
    if plan is not None:
        plan_bytes = canonical.jsonl(plan)
    builder = CaseBuilder(data, seed)
    cases = builder.build(plan if plan is not None
                          else read_jsonl(instance / "cases/plan.jsonl"))
    mark_rephrase(cases, seed)
    if callable(gold_sql_check):
        gold_sql_check = gold_sql_check(cases)
    review_ids = set(select_review_set(cases, seed))
    files: dict[str, bytes] = {
        "cases.jsonl": canonical.jsonl(cases),
        "review_set.jsonl": canonical.jsonl(review_record(c) for c in cases
                                            if c["case_id"] in review_ids),
        "rephrase_queue.jsonl": canonical.jsonl(rephrase_queue(cases)),
        "BUILD_REPORT.json": (canonical.dumps(build_report(cases, builder.rejections))
                              + "\n").encode(),
    }
    test = [c for c in cases if c["split"] == "test"]
    manifest = {
        "status": STATUS_UNFROZEN,
        "open_before_freeze": ["human review of review_set.jsonl",
                               "independent-model-family paraphrase and validation of "
                               "rephrase_queue.jsonl"],
        "case_schema_version": CASE_SCHEMA_VERSION, "validator_version": VALIDATOR_VERSION,
        "instance": {"instance_digest": data.meta["instance_digest"],
                     "authorization_outcome_digest": data.meta["authorization_outcome_digest"],
                     "config": data.meta["config"], "policy_version": data.meta["policy_version"]},
        "plan_sha256": canonical.sha256_bytes(plan_bytes),
        "metric_catalog_sha256": canonical.sha256_bytes(layer.catalog_bytes()),
        "sources_sha256": source_digests(),
        "files_sha256": {k: canonical.sha256_bytes(v) for k, v in sorted(files.items())},
        "counts": {"cases": len(cases), "test": len(test), "dev": len(cases) - len(test),
                   "review_set": len(review_ids),
                   "pending_rephrase_test": sum(1 for c in test
                                                if c["rephrase_status"] == PENDING_REPHRASE)},
        "gold_sql_check": gold_sql_check or {"status": "not_run"},
    }
    files["MANIFEST.json"] = (canonical.dumps(manifest) + "\n").encode()
    return files


def _machine_part(name: str, blob: bytes) -> bytes:
    """The bytes of a corpus file with the fields a human fills reset to empty, so a
    filled-in review set or rephrase queue still verifies against the code."""
    if name == "review_set.jsonl":
        recs = [json.loads(x) for x in blob.decode("utf-8").splitlines()]
        for r in recs:
            r["review"] = {"reviewed": None, "issue": None, "resolution": None}
        return canonical.jsonl(recs)
    if name == "rephrase_queue.jsonl":
        recs = [json.loads(x) for x in blob.decode("utf-8").splitlines()]
        for r in recs:
            r.update(rephrased_question=None, rephrased_by_model_family=None,
                     meaning_preserved_check=None)
        return canonical.jsonl(recs)
    return blob


def verify(cases_dir: Path, instance: Path,
           plan: list[dict[str, Any]] | None = None) -> list[str]:
    """Rebuild the corpus from ``instance`` and compare with ``cases_dir``. Returns the
    problems found (empty means the directory is exactly what the code produces, apart
    from the review and rephrase fields a human fills)."""
    manifest = json.loads((cases_dir / "MANIFEST.json").read_text("utf-8"))
    problems: list[str] = []
    for name, digest in manifest["files_sha256"].items():
        path = cases_dir / name
        if not path.exists():
            problems.append(f"{name}: missing")
        elif canonical.sha256_bytes(_machine_part(name, path.read_bytes())) != digest:
            problems.append(f"{name}: does not match the manifest")
    rebuilt = assemble(instance, manifest["gold_sql_check"], plan)
    for name, blob in sorted(rebuilt.items()):
        path = cases_dir / name
        if path.exists() and _machine_part(name, path.read_bytes()) != blob:
            problems.append(f"{name}: differs from a rebuild")
    return problems
