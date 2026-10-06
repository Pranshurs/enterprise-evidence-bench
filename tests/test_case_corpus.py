"""Corpus assembly: rephrase queue, review set, manifest, and what ``verify`` catches."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from eeb import canonical
from eeb.cases import corpus, rephrase
from eeb.cli import main
from eeb.db.load import read_jsonl
from tests.test_case_build import PROBE_SHARE_ON_SMALL, SLOTS_ON_SMALL


@pytest.fixture(scope="module")
def plan(instance_dir: Path) -> list[dict[str, Any]]:
    full = read_jsonl(instance_dir / "cases/plan.jsonl")
    keep: set[str] = set()
    for cls, n in SLOTS_ON_SMALL.items():
        keep |= set(sorted(s["case_id"] for s in full if s["class"] == cls)[:n])
    return [s for s in full if s["class"] not in SLOTS_ON_SMALL or s["case_id"] in keep]


@pytest.fixture(scope="module")
def corpus_files(instance_dir: Path, plan: list[dict[str, Any]]) -> dict[str, bytes]:
    return corpus.assemble(instance_dir, plan=plan, probe_share=PROBE_SHARE_ON_SMALL)


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
    test = [c for c in cases if c["split"] == "test"]
    num, den = corpus.REVIEW_TEST_FRACTION
    assert sum(1 for c in chosen if c["split"] == "test") * den >= len(test) * num
    assert {c["template_id"] for c in chosen} == {c["template_id"] for c in cases}
    assert {c["class"] for c in chosen if c["split"] == "test"} == {c["class"] for c in test}
    assert {c["principal_id"] for c in chosen} == {c["principal_id"] for c in cases}
    assert ({c["abstention_condition"] for c in chosen}
            == {c["abstention_condition"] for c in cases})
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
    assert corpus.assemble(instance_dir, plan=plan,
                           probe_share=PROBE_SHARE_ON_SMALL) == corpus_files


def test_verify_accepts_the_built_corpus(written: Path, instance_dir: Path,
                                         plan: list[dict[str, Any]]) -> None:
    assert corpus.verify(written, instance_dir, plan, PROBE_SHARE_ON_SMALL) == []


def test_verify_accepts_filled_in_review_and_rephrase_fields(
        written: Path, instance_dir: Path, plan: list[dict[str, Any]]) -> None:
    review = _records((written / "review_set.jsonl").read_bytes())
    review[0]["review"] = {"reviewed": True, "issue": "unit missing", "resolution": "fixed"}
    (written / "review_set.jsonl").write_bytes(canonical.jsonl(review))
    queue = _records((written / "rephrase_queue.jsonl").read_bytes())
    queue[0].update(rephrased_question="Another wording?", rephrased_by_model_family="other",
                    meaning_preserved_check=True)
    (written / "rephrase_queue.jsonl").write_bytes(canonical.jsonl(queue))
    assert corpus.verify(written, instance_dir, plan, PROBE_SHARE_ON_SMALL) == []


@pytest.mark.parametrize("name,field", [("cases.jsonl", "principal_id"),
                                        ("review_set.jsonl", "question"),
                                        ("rephrase_queue.jsonl", "question_canonical")])
def test_red_arm_verify_rejects_an_edited_machine_field(
        written: Path, instance_dir: Path, plan: list[dict[str, Any]], name: str,
        field: str) -> None:
    recs = _records((written / name).read_bytes())
    recs[0][field] = "edited by hand"
    (written / name).write_bytes(canonical.jsonl(recs))
    assert corpus.verify(written, instance_dir, plan, PROBE_SHARE_ON_SMALL) == [
        f"{name}: does not match the manifest", f"{name}: differs from a rebuild"]


def test_red_arm_verify_rejects_a_missing_file_and_a_stale_manifest(
        written: Path, instance_dir: Path, plan: list[dict[str, Any]]) -> None:
    (written / "BUILD_REPORT.json").unlink()
    assert corpus.verify(written, instance_dir, plan, PROBE_SHARE_ON_SMALL) == [
        "BUILD_REPORT.json: missing"]
    m = json.loads((written / "MANIFEST.json").read_text("utf-8"))
    m["sources_sha256"]["cases/validate.py"] = "0" * 64  # built by other validator code
    (written / "MANIFEST.json").write_text(canonical.dumps(m) + "\n", "utf-8")
    assert "MANIFEST.json: differs from a rebuild" in corpus.verify(
        written, instance_dir, plan, PROBE_SHARE_ON_SMALL)


def test_cli_refuses_to_overwrite_a_corpus(written: Path, instance_dir: Path) -> None:
    with pytest.raises(SystemExit, match="never overwritten"):
        main(["cases", "build", "--instance", str(instance_dir), "--out", str(written)])


# ------------------------------------------------------------------ provenance and freeze
def test_manifest_binds_generator_policy_config_and_content_gates(
        corpus_files: dict[str, bytes], instance_dir: Path) -> None:
    m = json.loads(corpus_files["MANIFEST.json"])
    meta = json.loads((instance_dir / "INSTANCE.json").read_text("utf-8"))
    inst = m["instance"]
    assert inst["generator_version"] == meta["generator_version"]
    assert inst["policy_sha256"] == canonical.sha256_bytes(
        (instance_dir / "policy.yaml").read_bytes())
    assert inst["config_sha256"] == canonical.sha256_bytes(
        canonical.dumps(meta["config"]).encode())
    assert "cases/rephrase.py" in m["sources_sha256"]
    gates = m["content_gates"]
    # The test sub-plan is below the Level-A minimums, and the manifest says so.
    assert gates["problems"] and set(gates) >= {"x_template_largest_pct",
                                                "x_template_top3_pct",
                                                "test_restricted_probe_cases"}


def test_queue_gives_the_paraphraser_anchors_but_no_gold(corpus_files: dict[str, bytes]
                                                         ) -> None:
    """The paraphraser sees the question and anchors taken from it, nothing else: no gold
    value, evidence or principal."""
    for e in _records(corpus_files["rephrase_queue.jsonl"]):
        assert set(e) == {"family_id", "case_ids", "class", "template_id",
                          "question_canonical", "must_preserve", "rephrased_question",
                          "rephrased_by_model_family", "meaning_preserved_check"}
        assert e["must_preserve"] == rephrase.must_preserve(e["question_canonical"])
        for kind, values in e["must_preserve"].items():
            if kind != "meaning":
                assert all(v.split(" (")[0] in e["question_canonical"] for v in values)


Q = ("What was the total order value of the lines from Broustrail Industrial Supply "
     "(SUP-0041) promised in 2024Q4 that were received after their promised date?")
P = ("For Broustrail Industrial Supply (SUP-0041), what is the total value of order lines "
     "promised in 2024Q4 that arrived late?")


def _status_dir(tmp_path: Path, **over: Any) -> Path:
    d = tmp_path / "c"
    d.mkdir()
    m = {"status": "UNFROZEN", "gold_sql_check": {"status": "passed"},
         "content_gates": {"problems": []}}
    m.update(over.get("manifest", {}))
    (d / "MANIFEST.json").write_text(json.dumps(m), "utf-8")
    review = [{"case_id": "C-1", "review": {"reviewed": True, "issue": None,
                                            "resolution": None}},
              {"case_id": "C-2", "review": {"reviewed": True, "issue": "unit",
                                            "resolution": "added EUR"}}]
    for r in over.get("review", []):
        review[0]["review"].update(r)
    (d / "review_set.jsonl").write_bytes(canonical.jsonl(review))
    entry = {"family_id": "f", "question_canonical": Q, "rephrased_question": P,
             "rephrased_by_model_family": "family-b"}
    entry.update(over.get("entry", {}))
    (d / "rephrase_queue.jsonl").write_bytes(canonical.jsonl([entry]))
    return d


def test_freeze_eligible_only_when_every_human_step_is_done(tmp_path: Path) -> None:
    s = corpus.human_status(_status_dir(tmp_path), reference_family="family-a")
    assert s["freeze_eligible"] is True and s["freeze_blockers"] == []


@pytest.mark.parametrize(("over", "ref", "blocker"), [
    ({"review": [{"reviewed": None}]}, "family-a", "1/2 records reviewed"),
    ({"review": [{"issue": "wrong unit"}]}, "family-a", "1 issues without a resolution"),
    ({"entry": {"rephrased_question": None}}, "family-a", "PENDING_REPHRASE"),
    ({"entry": {"rephrased_by_model_family": "family-a"}}, "family-a", "rejected"),
    ({"entry": {"rephrased_question": P.replace("2024Q4", "2025Q4")}}, "family-a",
     "rejected"),
    ({}, None, "reference agent's model family is not stated"),
    ({"manifest": {"gold_sql_check": {"status": "not_run"}}}, "family-a",
     "gold SQL check: not_run"),
    ({"manifest": {"content_gates": {"problems": ["x"]}}}, "family-a", "content gates: x"),
], ids=["unreviewed", "issue-open", "pending", "same-family", "changed-period",
        "no-reference-family", "gold-not-run", "content-gate"])
def test_red_arm_each_open_step_blocks_freezing(tmp_path: Path, over: dict[str, Any],
                                                ref: str | None, blocker: str) -> None:
    s = corpus.human_status(_status_dir(tmp_path, **over), reference_family=ref)
    assert s["freeze_eligible"] is False
    assert any(blocker in b for b in s["freeze_blockers"]), s["freeze_blockers"]


def test_built_corpus_is_not_freeze_eligible(written: Path) -> None:
    s = corpus.human_status(written, reference_family="family-a")
    assert s["freeze_eligible"] is False
    assert s["review"]["reviewed"] == 0
    assert s["rephrase"]["counts"] == {"PENDING_REPHRASE": s["rephrase"]["entries"]}
