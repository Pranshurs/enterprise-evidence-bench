"""The corpus freeze event (spec §11.1; design decision 2026-10-06).

``freeze`` is allowed only when ``verify`` finds the directory to be exactly what the code
builds, every human-in-the-loop step is done (``human_status``: reviews complete with
issues resolved, every queued question paraphrased by an independent family and
mechanically consistent, gold SQL passed, no content gate problem), and the repository has
no uncommitted change. It then

- writes ``cases.final.jsonl``: the cases with paraphrases applied (``question`` is the
  paraphrase, ``question_canonical`` is kept, ``rephrase_status`` is ``REPHRASED`` and
  ``provenance`` records the paraphrasing model) and ``reviewed_by_human`` set for
  reviewed cases;
- writes ``FREEZE.json``, binding every input and output by digest, with the commit;
- makes every file read-only.

After the freeze the corpus is immutable for the rest of Level A. A benchmark defect found
later is a recorded finding and a versioned corpus change, never a silent rebuild.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from eeb import canonical
from eeb.cases import corpus
from eeb.cases.paraphrase import provenance_problems
from eeb.db.load import read_jsonl
from eeb.metrics import layer

FROZEN = "FROZEN"
REPHRASED = "REPHRASED"


class FreezeError(RuntimeError):
    pass


def apply_human_steps(cases: list[dict[str, Any]], queue: list[dict[str, Any]],
                      review: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The final cases: paraphrases applied, reviewed cases marked."""
    out = [json.loads(json.dumps(c)) for c in cases]
    by_id = {c["case_id"]: c for c in out}
    for e in queue:
        for cid in e["case_ids"]:
            c = by_id[cid]
            c["question"] = e["rephrased_question"]
            c["rephrase_status"] = REPHRASED
            c["provenance"]["rephrased_by"] = e["rephrased_by_model_family"]
    for r in review:
        if r["review"]["reviewed"] is True:
            by_id[r["case_id"]]["provenance"]["reviewed_by_human"] = True
    return out


def _sha(path: Path) -> str:
    return canonical.sha256_bytes(path.read_bytes())


def freeze(cases_dir: Path, instance: Path, spec: Path, reference_family: str,
           commit: str, tree_clean: bool) -> dict[str, Any]:
    if (cases_dir / "FREEZE.json").exists():
        raise FreezeError("already frozen; a change needs a new, versioned corpus")
    if not tree_clean:
        raise FreezeError("the repository has uncommitted changes")
    try:
        problems = corpus.verify(cases_dir, instance)
    except Exception as e:  # a corpus that cannot be rebuilt is not frozen
        raise FreezeError(f"verify could not rebuild the corpus: {e}") from e
    if problems:
        raise FreezeError("verify: " + "; ".join(problems))
    status = corpus.human_status(cases_dir, reference_family)
    if not status["freeze_eligible"]:
        raise FreezeError("not eligible: " + "; ".join(status["freeze_blockers"]))
    prov = cases_dir / "rephrase_provenance.jsonl"
    runs: list[Any] | None = None
    if prov.exists():
        try:
            runs = [json.loads(x) for x in prov.read_text("utf-8").splitlines() if x.strip()]
        except ValueError:
            runs = ["unparseable"]
    bad = provenance_problems(read_jsonl(cases_dir / "rephrase_queue.jsonl"), runs)
    if bad:
        raise FreezeError("provenance: " + "; ".join(bad[:10]))
    manifest = json.loads((cases_dir / "MANIFEST.json").read_text("utf-8"))
    cases = read_jsonl(cases_dir / "cases.jsonl")
    queue = read_jsonl(cases_dir / "rephrase_queue.jsonl")
    review = read_jsonl(cases_dir / "review_set.jsonl")
    final = apply_human_steps(cases, queue, review)
    (cases_dir / "cases.final.jsonl").write_bytes(canonical.jsonl(final))
    test = [c for c in final if c["split"] == "test"]
    record = {
        "status": FROZEN, "commit": commit,
        "spec_sha256": _sha(spec),
        "instance": manifest["instance"],
        "metric_catalog_sha256": canonical.sha256_bytes(layer.catalog_bytes()),
        "plan_sha256": manifest["plan_sha256"],
        "sources_sha256": manifest["sources_sha256"],
        "files_sha256": {name: _sha(cases_dir / name) for name in sorted(
            ["cases.jsonl", "cases.final.jsonl", "review_set.jsonl", "rephrase_queue.jsonl",
             "BUILD_REPORT.json", "MANIFEST.json"]
            + (["rephrase_provenance.jsonl"] if prov.exists() else []))},
        "canonical_cases_sha256": _sha(cases_dir / "cases.jsonl"),
        "final_cases_sha256": _sha(cases_dir / "cases.final.jsonl"),
        "review": status["review"],
        "rephrase": {"entries": len(queue), "counts": status["rephrase"]["counts"],
                     "models": sorted({e["rephrased_by_model_family"] for e in queue}),
                     "runs": read_jsonl(prov) if prov.exists() else [],
                     "reference_family": reference_family},
        "counts": {"cases": len(final), "test": len(test), "dev": len(final) - len(test),
                   "rephrased_test": sum(1 for c in test if c["rephrase_status"] == REPHRASED),
                   "reviewed_test": sum(1 for c in test
                                        if c["provenance"]["reviewed_by_human"])},
        "gold_sql_check": manifest["gold_sql_check"],
        "content_gates": manifest["content_gates"],
    }
    (cases_dir / "FREEZE.json").write_text(canonical.dumps(record) + "\n", "utf-8")
    for p in cases_dir.iterdir():
        p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return record


def verify_frozen(cases_dir: Path) -> list[str]:
    """Problems with a frozen corpus directory (empty: intact)."""
    path = cases_dir / "FREEZE.json"
    if not path.exists():
        return ["not frozen"]
    rec = json.loads(path.read_text("utf-8"))
    out = []
    for name, digest in rec["files_sha256"].items():
        f = cases_dir / name
        if not f.exists():
            out.append(f"{name}: missing")
            continue
        if _sha(f) != digest:
            out.append(f"{name}: changed since the freeze")
        if os.access(f, os.W_OK) and f.stat().st_mode & 0o222:
            out.append(f"{name}: writable")
    if rec["final_cases_sha256"] != rec["files_sha256"].get("cases.final.jsonl"):
        out.append("FREEZE.json: final case digest inconsistent")
    return out
