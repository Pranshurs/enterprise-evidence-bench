"""The freeze event: refused until every step is done; then the final cases carry the
paraphrases and review marks, FREEZE.json binds everything, files are read-only, and any
later change is detected.

The fixture corpus is a sub-plan below the Level-A minimums, so its content gates block a
freeze (checked). The success path filters only that blocker; every other check is real.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from eeb import canonical
from eeb.cases import corpus, freeze
from eeb.cases.paraphrase import paraphrase_queue, provenance_record
from eeb.db.load import read_jsonl
from eeb.harness.upstreams import ScriptedUpstream
from tests.test_case_build import PROBE_SHARE_ON_SMALL, SLOTS_ON_SMALL

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built(instance_dir: Path) -> dict[str, bytes]:
    full = read_jsonl(instance_dir / "cases/plan.jsonl")
    keep: set[str] = set()
    for cls, n in SLOTS_ON_SMALL.items():
        keep |= set(sorted(s["case_id"] for s in full if s["class"] == cls)[:n])
    plan = [s for s in full if s["class"] not in SLOTS_ON_SMALL or s["case_id"] in keep]
    return corpus.assemble(instance_dir, {"status": "passed"}, plan,
                           probe_share=PROBE_SHARE_ON_SMALL)


def _write(built: dict[str, bytes], d: Path) -> Path:
    d.mkdir()
    for k, v in built.items():
        (d / k).write_bytes(v)
    return d


def _complete_human_steps(d: Path) -> None:
    review = read_jsonl(d / "review_set.jsonl")
    for r in review:
        r["review"] = {"reviewed": True, "issue": None, "resolution": None}
    (d / "review_set.jsonl").write_bytes(canonical.jsonl(review))
    queue = read_jsonl(d / "rephrase_queue.jsonl")
    run = paraphrase_queue(
        queue, ScriptedUpstream(script=lambda api, body: "Could you tell me: " + body[
            "messages"][0]["content"].split("Question: ", 1)[1].split("\n")[0]),
        "anthropic.messages", "model-x", "anthropic", "openai")
    assert run.counts["failed"] == 0
    (d / "rephrase_queue.jsonl").write_bytes(canonical.jsonl(queue))
    rec = provenance_record("model-x", "anthropic", "anthropic.messages", "pending", 0, run,
                            "abc", "2026-10-06T00:00:00+00:00")
    (d / "rephrase_provenance.jsonl").write_text(json.dumps(rec) + "\n")


@pytest.fixture()
def ready(built: dict[str, bytes], tmp_path: Path, instance_dir: Path,
          monkeypatch: pytest.MonkeyPatch) -> Path:
    d = _write(built, tmp_path / "c")
    _complete_human_steps(d)
    real_status = corpus.human_status

    def status(cases_dir: Path, ref: str | None = None) -> dict[str, Any]:
        s = real_status(cases_dir, ref)
        s["freeze_blockers"] = [b for b in s["freeze_blockers"]
                                if not b.startswith("content gates")]
        s["freeze_eligible"] = not s["freeze_blockers"]
        return s
    monkeypatch.setattr(corpus, "human_status", status)
    monkeypatch.setattr(corpus, "verify", lambda c, i: [])   # the sub-plan's verify is
    return d                                                 # tested in test_case_corpus


def test_freeze_refused_until_every_step_is_done(built: dict[str, bytes], tmp_path: Path,
                                                 instance_dir: Path) -> None:
    d = _write(built, tmp_path / "c")
    spec = ROOT / "docs/spec.md"
    with pytest.raises(freeze.FreezeError, match="uncommitted"):
        freeze.freeze(d, instance_dir, spec, "openai", "abc", tree_clean=False)
    with pytest.raises(freeze.FreezeError) as e:
        freeze.freeze(d, instance_dir, spec, "openai", "abc", tree_clean=True)
    assert "verify" in str(e.value) or "not eligible" in str(e.value)
    assert not (d / "FREEZE.json").exists() and not (d / "cases.final.jsonl").exists()


def test_freeze_refused_with_reviews_open(ready: Path, instance_dir: Path) -> None:
    review = read_jsonl(ready / "review_set.jsonl")
    review[0]["review"] = {"reviewed": True, "issue": "unit missing", "resolution": None}
    (ready / "review_set.jsonl").write_bytes(canonical.jsonl(review))
    with pytest.raises(freeze.FreezeError, match="issues without a resolution"):
        freeze.freeze(ready, instance_dir, ROOT / "docs/spec.md", "openai", "abc", True)


def test_freeze_writes_final_cases_and_binds_everything(ready: Path,
                                                        instance_dir: Path) -> None:
    rec = freeze.freeze(ready, instance_dir, ROOT / "docs/spec.md", "openai", "abc123", True)
    assert rec["status"] == "FROZEN" and rec["commit"] == "abc123"
    assert rec["spec_sha256"] == "3419920460863060ce53cfcfbc9750ecda341a7fc8da96279535fc9b02229e4b"
    for k in ("instance", "metric_catalog_sha256", "canonical_cases_sha256",
              "final_cases_sha256", "review", "rephrase", "gold_sql_check"):
        assert rec[k], k
    assert rec["instance"]["generator_version"] and rec["instance"]["policy_sha256"]
    assert rec["rephrase"]["models"] == ["anthropic:model-x"]
    canon = {c["case_id"]: c for c in read_jsonl(ready / "cases.jsonl")}
    final = {c["case_id"]: c for c in read_jsonl(ready / "cases.final.jsonl")}
    queued = {cid for e in read_jsonl(ready / "rephrase_queue.jsonl") for cid in e["case_ids"]}
    reviewed = {r["case_id"] for r in read_jsonl(ready / "review_set.jsonl")}
    assert queued and reviewed
    for cid, c in final.items():
        assert c["question_canonical"] == canon[cid]["question_canonical"]
        if cid in queued:
            assert c["question"].startswith("Could you tell me: ")
            assert c["rephrase_status"] == "REPHRASED"
            assert c["provenance"]["rephrased_by"] == "anthropic:model-x"
        else:
            assert c["question"] == canon[cid]["question"]
        assert c["provenance"]["reviewed_by_human"] is (cid in reviewed)
        assert {k: v for k, v in c.items() if k not in ("question", "rephrase_status",
                                                        "provenance")} == {
            k: v for k, v in canon[cid].items() if k not in ("question", "rephrase_status",
                                                            "provenance")}
    for p in ready.iterdir():
        assert not p.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH), p.name
    assert freeze.verify_frozen(ready) == []
    with pytest.raises(freeze.FreezeError, match="already frozen"):
        freeze.freeze(ready, instance_dir, ROOT / "docs/spec.md", "openai", "abc123", True)


@pytest.mark.parametrize("target", ["cases.final.jsonl", "review_set.jsonl", "MANIFEST.json"])
def test_red_arm_change_after_freeze_is_detected(ready: Path, instance_dir: Path,
                                                 target: str) -> None:
    freeze.freeze(ready, instance_dir, ROOT / "docs/spec.md", "openai", "abc", True)
    f = ready / target
    f.chmod(0o644)
    f.write_bytes(f.read_bytes().replace(b"\n", b"\n ", 1))
    problems = freeze.verify_frozen(ready)
    assert f"{target}: changed since the freeze" in problems
    assert f"{target}: writable" in problems


def test_red_arm_missing_file_after_freeze(ready: Path, instance_dir: Path) -> None:
    rec = freeze.freeze(ready, instance_dir, ROOT / "docs/spec.md", "openai", "abc", True)
    (ready / "cases.final.jsonl").chmod(0o644)
    (ready / "cases.final.jsonl").unlink()
    assert "cases.final.jsonl: missing" in freeze.verify_frozen(ready)
    assert json.loads((ready / "FREEZE.json").read_text())["files_sha256"] == rec["files_sha256"]


def test_paraphrase_pilot_limit_and_provenance() -> None:
    from eeb.cases.paraphrase import provenance_record
    q = [{"family_id": f"f{i}", "case_ids": [f"C-{i}"],
          "question_canonical": "What was the total invoiced amount from Ab Co (SUP-0001) "
                                f"in 2024Q{i % 4 + 1}?",
          "must_preserve": {}, "rephrased_question": None, "rephrased_by_model_family": None,
          "meaning_preserved_check": None} for i in range(8)]
    for e in q:
        from eeb.cases import rephrase
        e["must_preserve"] = rephrase.must_preserve(str(e["question_canonical"]))
    run = paraphrase_queue(q, ScriptedUpstream(script=lambda a, b: "Tell me " + b[
        "messages"][0]["content"].split("Question: ", 1)[1].split("\n")[0]),
        "anthropic.messages", "m", "anthropic", "openai", limit=3)
    assert run.counts["asked"] == 3 and sum(1 for e in q if e["rephrased_question"]) == 3
    assert len(run.produced) == 3
    rec = provenance_record("m", "anthropic", "anthropic.messages", "pending", 3, run,
                            "abc", "2026-10-06T00:00:00+00:00")
    assert rec["parameters"] == {"temperature": 0, "max_tokens": 300}
    blob = json.dumps(rec).lower()
    assert "key" not in blob and "authorization" not in blob and "header" not in blob



# ------------------------------------------------------------ provenance binds the paraphrases
def _set_provenance(d: Path, text: str | None) -> None:
    p = d / "rephrase_provenance.jsonl"
    if text is None:
        p.unlink()
    else:
        p.write_text(text)


def _first_run(d: Path) -> dict[str, Any]:
    return json.loads((d / "rephrase_provenance.jsonl").read_text().splitlines()[0])


@pytest.mark.parametrize(("edit", "expected"), [
    ("missing", "rephrase provenance is missing"),
    ("empty", "rephrase provenance is empty"),
    ("unparseable", "is malformed"),
    ("no-produced", "is malformed"),
    ("bad-entry", "has a malformed entry"),
    ("other-model", "names a model it did not run"),
    ("text-edited", "is not bound by the provenance"),
    ("entry-dropped", "is not bound by the provenance"),
], ids=lambda x: x if " " not in x else "")
def test_red_arm_freeze_refused_without_binding_provenance(
        ready: Path, instance_dir: Path, edit: str, expected: str) -> None:
    run = _first_run(ready)
    if edit == "missing":
        _set_provenance(ready, None)
    elif edit == "empty":
        _set_provenance(ready, "")
    elif edit == "unparseable":
        _set_provenance(ready, "{not json\n")
    elif edit == "no-produced":
        del run["produced"]
        _set_provenance(ready, json.dumps(run) + "\n")
    elif edit == "bad-entry":
        run["produced"][0] = {"family_id": 1}
        _set_provenance(ready, json.dumps(run) + "\n")
    elif edit == "other-model":
        run["model"] = "another-model"
        _set_provenance(ready, json.dumps(run) + "\n")
    elif edit == "text-edited":
        queue = read_jsonl(ready / "rephrase_queue.jsonl")
        queue[0]["rephrased_question"] += " Please."
        (ready / "rephrase_queue.jsonl").write_bytes(canonical.jsonl(queue))
    elif edit == "entry-dropped":
        run["produced"] = run["produced"][1:]
        _set_provenance(ready, json.dumps(run) + "\n")
    with pytest.raises(freeze.FreezeError, match=expected):
        freeze.freeze(ready, instance_dir, ROOT / "docs/spec.md", "openai", "abc", True)
    assert not (ready / "FREEZE.json").exists()


def test_green_arm_bound_provenance_is_accepted_and_frozen(ready: Path,
                                                           instance_dir: Path) -> None:
    from eeb.cases.paraphrase import provenance_problems
    runs = [json.loads(x) for x in (ready / "rephrase_provenance.jsonl").read_text()
            .splitlines()]
    assert provenance_problems(read_jsonl(ready / "rephrase_queue.jsonl"), runs) == []
    rec = freeze.freeze(ready, instance_dir, ROOT / "docs/spec.md", "openai", "abc", True)
    assert "rephrase_provenance.jsonl" in rec["files_sha256"]
    assert rec["rephrase"]["runs"] == runs
