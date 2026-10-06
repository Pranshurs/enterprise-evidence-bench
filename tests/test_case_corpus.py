"""Corpus assembly: rephrase queue, review set, manifest, and what ``verify`` catches."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from eeb import canonical
from eeb.cases import corpus
from eeb.cli import main
from eeb.db.load import read_jsonl
from tests.test_case_build import SLOTS_ON_SMALL


@pytest.fixture(scope="module")
def plan(instance_dir: Path) -> list[dict[str, Any]]:
    full = read_jsonl(instance_dir / "cases/plan.jsonl")
    keep: set[str] = set()
    for cls, n in SLOTS_ON_SMALL.items():
        keep |= set(sorted(s["case_id"] for s in full if s["class"] == cls)[:n])
    return [s for s in full if s["class"] not in SLOTS_ON_SMALL or s["case_id"] in keep]


@pytest.fixture(scope="module")
def corpus_files(instance_dir: Path, plan: list[dict[str, Any]]) -> dict[str, bytes]:
    return corpus.assemble(instance_dir, plan=plan)


def _records(blob: bytes) -> list[dict[str, Any]]:
    return [json.loads(x) for x in blob.decode("utf-8").splitlines()]


@pytest.fixture(scope="module")
def cases(corpus_files: dict[str, bytes]) -> list[dict[str, Any]]:
    return _records(corpus_files["cases.jsonl"])


@pytest.fixture()
def written(corpus_files: dict[str, bytes], tmp_path: Path) -> Path:
    out = tmp_path / "cases"
    out.mkdir()
    for name, blob in corpus_files.items():
        (out / name).write_bytes(blob)
    return out


# ------------------------------------------------------------------ rephrase queue
def test_a_fifth_of_every_classes_test_cases_awaits_rephrasing(
        cases: list[dict[str, Any]]) -> None:
    test = [c for c in cases if c["split"] == "test"]
    pending = [c for c in test if c["rephrase_status"] == corpus.PENDING_REPHRASE]
    assert len(pending) * 5 >= len(test)
    for cls, n in Counter(c["class"] for c in test).items():
        assert sum(1 for c in pending if c["class"] == cls) >= math.ceil(n / 5), cls
    assert all(c["rephrase_status"] == "not_required" for c in cases if c["split"] == "dev")
    # Until a paraphrase is supplied the question is still the template rendering.
    assert all(c["question"] == c["question_canonical"] for c in cases)


def test_group_members_are_rephrased_together(cases: list[dict[str, Any]]) -> None:
    groups: dict[str, set[str]] = {}
    for c in cases:
        if c["group_id"]:
            groups.setdefault(c["group_id"], set()).add(c["rephrase_status"])
    assert groups and all(len(s) == 1 for s in groups.values())
    assert any(s == {corpus.PENDING_REPHRASE} for s in groups.values())


def test_rephrase_queue_lists_each_pending_question_once(corpus_files: dict[str, bytes],
                                                         cases: list[dict[str, Any]]) -> None:
    queue = _records(corpus_files["rephrase_queue.jsonl"])
    pending = {c["case_id"] for c in cases if c["rephrase_status"] == corpus.PENDING_REPHRASE}
    assert sorted(i for e in queue for i in e["case_ids"]) == sorted(pending)
    assert len({e["family_id"] for e in queue}) == len(queue)
    assert all(e["rephrased_question"] is None and e["rephrased_by_model_family"] is None
               and e["meaning_preserved_check"] is None for e in queue)


# ------------------------------------------------------------------ review set
def test_review_set_is_stratified(corpus_files: dict[str, bytes],
                                  cases: list[dict[str, Any]]) -> None:
    review = _records(corpus_files["review_set.jsonl"])
    ids = {r["case_id"] for r in review}
    chosen = [c for c in cases if c["case_id"] in ids]
    assert corpus.REVIEW_TARGET <= len(review) <= corpus.REVIEW_TARGET + 15
    assert {c["template_id"] for c in chosen} == {c["template_id"] for c in cases}
    assert {(c["class"], c["split"]) for c in chosen} == {(c["class"], c["split"])
                                                          for c in cases}
    assert {c["expected_outcome"] for c in chosen} == {c["expected_outcome"] for c in cases}
    assert sum(1 for c in chosen if c["overlays"]["injection"]) >= 3
    assert sum(1 for c in chosen if c["overlays"]["ool"] and c["class"] == "X") >= 3
    assert sum(1 for c in chosen if c["overlays"]["ool"] and c["class"] == "S") >= 3
    pairs = Counter(c["group_id"] for c in chosen if c["group_id"])
    assert len(pairs) >= 3 and set(pairs.values()) == {2}
    assert all(r["review"] == {"reviewed": None, "issue": None, "resolution": None}
               for r in review)


def test_review_records_show_where_each_value_comes_from(corpus_files: dict[str, bytes]) -> None:
    shown: Counter[str] = Counter()
    for r in _records(corpus_files["review_set.jsonl"]):
        for f in r["facts"]:
            key = {"sql": "gold_sql", "doc": "document", "derived": "derived"}[f["source"]]
            assert f[key]
            shown[f["source"]] += 1
    assert all(shown[s] > 0 for s in ("sql", "doc", "derived"))


# ------------------------------------------------------------------ manifest and verify
def test_manifest_binds_instance_code_and_files(corpus_files: dict[str, bytes],
                                                instance_dir: Path) -> None:
    m = json.loads(corpus_files["MANIFEST.json"])
    meta = json.loads((instance_dir / "INSTANCE.json").read_text("utf-8"))
    assert m["status"] == "UNFROZEN" and len(m["open_before_freeze"]) == 2
    assert m["instance"]["instance_digest"] == meta["instance_digest"]
    assert m["files_sha256"] == {k: canonical.sha256_bytes(v)
                                 for k, v in sorted(corpus_files.items())
                                 if k != "MANIFEST.json"}
    assert set(m["sources_sha256"]) == set(corpus.SOURCES)
    assert m["gold_sql_check"] == {"status": "not_run"}
    assert m["counts"]["cases"] == m["counts"]["dev"] + m["counts"]["test"]


def test_no_corpus_file_names_a_machine_or_a_time(corpus_files: dict[str, bytes]) -> None:
    needles = [str(Path.home()).encode(), b"/private/tmp", b"/Users/", b"/home/", b"/tmp/"]
    assert [k for k, v in corpus_files.items() if any(n in v for n in needles)] == []


def test_assembly_is_deterministic(corpus_files: dict[str, bytes], instance_dir: Path,
                                   plan: list[dict[str, Any]]) -> None:
    assert corpus.assemble(instance_dir, plan=plan) == corpus_files


def test_verify_accepts_the_built_corpus(written: Path, instance_dir: Path,
                                         plan: list[dict[str, Any]]) -> None:
    assert corpus.verify(written, instance_dir, plan) == []


def test_verify_accepts_filled_in_review_and_rephrase_fields(
        written: Path, instance_dir: Path, plan: list[dict[str, Any]]) -> None:
    review = _records((written / "review_set.jsonl").read_bytes())
    review[0]["review"] = {"reviewed": True, "issue": "unit missing", "resolution": "fixed"}
    (written / "review_set.jsonl").write_bytes(canonical.jsonl(review))
    queue = _records((written / "rephrase_queue.jsonl").read_bytes())
    queue[0].update(rephrased_question="Another wording?", rephrased_by_model_family="other",
                    meaning_preserved_check=True)
    (written / "rephrase_queue.jsonl").write_bytes(canonical.jsonl(queue))
    assert corpus.verify(written, instance_dir, plan) == []


@pytest.mark.parametrize("name,field", [("cases.jsonl", "principal_id"),
                                        ("review_set.jsonl", "question"),
                                        ("rephrase_queue.jsonl", "question_canonical")])
def test_red_arm_verify_rejects_an_edited_machine_field(
        written: Path, instance_dir: Path, plan: list[dict[str, Any]], name: str,
        field: str) -> None:
    recs = _records((written / name).read_bytes())
    recs[0][field] = "edited by hand"
    (written / name).write_bytes(canonical.jsonl(recs))
    assert corpus.verify(written, instance_dir, plan) == [
        f"{name}: does not match the manifest", f"{name}: differs from a rebuild"]


def test_red_arm_verify_rejects_a_missing_file_and_a_stale_manifest(
        written: Path, instance_dir: Path, plan: list[dict[str, Any]]) -> None:
    (written / "BUILD_REPORT.json").unlink()
    assert corpus.verify(written, instance_dir, plan) == ["BUILD_REPORT.json: missing"]
    m = json.loads((written / "MANIFEST.json").read_text("utf-8"))
    m["sources_sha256"]["cases/validate.py"] = "0" * 64  # built by other validator code
    (written / "MANIFEST.json").write_text(canonical.dumps(m) + "\n", "utf-8")
    assert "MANIFEST.json: differs from a rebuild" in corpus.verify(written, instance_dir, plan)


def test_cli_refuses_to_overwrite_a_corpus(written: Path, instance_dir: Path) -> None:
    with pytest.raises(SystemExit, match="never overwritten"):
        main(["cases", "build", "--instance", str(instance_dir), "--out", str(written)])
