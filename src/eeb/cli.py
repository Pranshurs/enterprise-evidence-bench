"""Command line: ``eeb build``, ``eeb verify``, ``eeb cases`` and ``eeb run``.

``build`` is the only supported way to produce an instance. It generates into a temporary
sibling directory, builds a fresh database, and runs the policy agreement and hardening
checks. It moves the directory into place only if every check passes. A disagreement fails
generation (spec §5.2).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from eeb import canonical
from eeb.cases import corpus
from eeb.cases.dbcheck import check_case_gold_sql
from eeb.db import agreement
from eeb.db.gold_check import check_gold_sql
from eeb.db.load import build_database, drop, export_tables, read_instance, read_jsonl
from eeb.generator.core import SCALES, Config
from eeb.generator.instance import build_instance_files, write_files


def _dsn(arg: str | None) -> str:
    dsn = arg or os.environ.get("EEB_PG_ADMIN_DSN")
    if not dsn:
        sys.exit("a Postgres admin DSN is required (--pg-dsn or EEB_PG_ADMIN_DSN)")
    return dsn


def validate(instance: Path, dsn: str, ns: str, keep_db: bool) -> dict[str, object]:
    meta = read_instance(instance)
    build_database(dsn, instance, ns)
    try:
        exported = export_tables(dsn, ns)
        mismatched = sorted(k for k, v in exported.items()
                            if canonical.sha256_bytes(v) != meta["files"][k])
        result = agreement.check(dsn, instance, ns)
        gold = check_gold_sql(dsn, instance, ns)
    finally:
        if not keep_db:
            drop(dsn, ns)
    return {
        "instance_digest": meta["instance_digest"],
        "database_reload_identical": not mismatched,
        "reload_mismatches": mismatched,
        "authorization_outcome_digest": {"recorded": result.recorded_digest,
                                         "oracle": result.oracle_digest,
                                         "database": result.db_digest,
                                         "verifier_twins": result.twin_digest},
        "twin_disagreements": result.twin_disagreements,
        "service_outcome_digest": result.service_digest,
        "service_problems": result.service_problems,
        "disagreements": result.disagreements,
        "hardening_problems": result.hardening,
        "gold_sql": gold,
        "passed": result.ok and not mismatched and not gold["mismatches"]
        and gold["sql_facts_checked"] > 0,
    }


def cmd_build(args: argparse.Namespace) -> int:
    dsn = _dsn(args.pg_dsn)
    out = Path(args.out)
    if out.exists():
        sys.exit(f"{out} already exists; instances are never overwritten")
    tmp = out.with_name(out.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    files = build_instance_files(Config(seed=args.seed, scale=args.scale))
    write_files(files, tmp)
    meta = json.loads(files["INSTANCE.json"])
    ns = "eeb_" + meta["instance_digest"][:12]
    report = validate(tmp, dsn, ns, args.keep_db)
    if not report["passed"]:
        print(json.dumps(report, indent=2), file=sys.stderr)
        shutil.rmtree(tmp)
        print("GENERATION FAILED: enforcement/oracle disagreement, gold mismatch or failed check",
              file=sys.stderr)
        return 1
    (tmp / "VALIDATION.json").write_text(canonical.dumps(report) + "\n", "utf-8")
    tmp.rename(out)
    print(json.dumps({"instance": str(out), "instance_digest": meta["instance_digest"],
                      "authorization_outcome_digest": meta["authorization_outcome_digest"],
                      "database": ns if args.keep_db else None}, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    dsn = _dsn(args.pg_dsn)
    instance = Path(args.instance)
    meta = read_instance(instance)
    ns = "eeb_v" + meta["instance_digest"][:11]
    report = validate(instance, dsn, ns, keep_db=False)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


class GoldSqlError(Exception):
    pass


def cmd_cases_build(args: argparse.Namespace) -> int:
    instance, out = Path(args.instance), Path(args.out)
    if out.exists():
        sys.exit(f"{out} already exists; case corpora are never overwritten")
    check = None
    if args.pg_dsn or os.environ.get("EEB_PG_ADMIN_DSN"):
        dsn = _dsn(args.pg_dsn)
        ns = "eeb_c" + read_instance(instance)["instance_digest"][:11]

        def check(cases: list[dict[str, Any]]) -> dict[str, Any]:
            build_database(dsn, instance, ns)
            try:
                r = check_case_gold_sql(dsn, ns, cases)
            finally:
                drop(dsn, ns)
            if r["problems"]:
                raise GoldSqlError(r)
            return {"status": "passed", "sql_facts_checked": r["sql_facts_checked"],
                    "restricted_probes_checked": r["restricted_probes_checked"],
                    "principals": r["principals"]}
    try:
        files = corpus.assemble(instance, check)
    except corpus.CorpusGateError as e:
        print(f"CASE BUILD FAILED: {e}", file=sys.stderr)
        return 1
    except GoldSqlError as e:
        print(json.dumps(e.args[0], indent=2), file=sys.stderr)
        print("CASE BUILD FAILED: a SQL gold fact does not hold under its principal's login",
              file=sys.stderr)
        return 1
    tmp = out.with_name(out.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for name, blob in files.items():
        (tmp / name).write_bytes(blob)
    tmp.rename(out)
    print(files["MANIFEST.json"].decode("utf-8"), end="")
    return 0


def cmd_cases_verify(args: argparse.Namespace) -> int:
    """Exit 0 when the directory is exactly what the code builds from the instance. The
    human-in-the-loop status and freeze eligibility are reported, not required."""
    problems = corpus.verify(Path(args.cases), Path(args.instance))
    report = {"problems": problems, "passed": not problems,
              **corpus.human_status(Path(args.cases), args.reference_family)}
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


def cmd_cases_rephrase(args: argparse.Namespace) -> int:
    """Paraphrase queued questions with an independent model family (Anthropic). The key is
    read from ANTHROPIC_API_KEY and never written anywhere."""
    from eeb.cases.paraphrase import dumps_queue, paraphrase_queue, provenance_record
    from eeb.harness.upstreams import AnthropicUpstream

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set")
    path = Path(args.cases) / "rephrase_queue.jsonl"
    entries = read_jsonl(path)
    counts = paraphrase_queue(entries, AnthropicUpstream(api_key=key), "anthropic.messages",
                              args.model, "anthropic", args.reference_family, args.only,
                              args.limit)
    path.write_bytes(dumps_queue(entries))
    import datetime as dt
    rec = provenance_record(args.model, "anthropic", "anthropic.messages", args.only,
                            args.limit, counts, _git_head(),
                            dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
    with (Path(args.cases) / "rephrase_provenance.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    print(json.dumps(counts, indent=2))
    return 0


def _tree_clean() -> bool:
    import subprocess
    r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                       check=False, cwd=Path(__file__).resolve().parents[2])
    return r.returncode == 0 and not r.stdout.strip()


def cmd_cases_freeze(args: argparse.Namespace) -> int:
    from eeb.cases.freeze import FreezeError, freeze
    spec = Path(args.spec) if args.spec else Path(__file__).resolve().parents[2] / "docs/spec.md"
    try:
        rec = freeze(Path(args.cases), Path(args.instance), spec, args.reference_family,
                     _git_head(), _tree_clean())
    except FreezeError as e:
        print(f"FREEZE REFUSED: {e}", file=sys.stderr)
        return 1
    print(json.dumps({k: rec[k] for k in ("status", "commit", "canonical_cases_sha256",
                                          "final_cases_sha256", "counts")}, indent=2))
    return 0


def cmd_cases_verify_frozen(args: argparse.Namespace) -> int:
    from eeb.cases.freeze import verify_frozen
    problems = verify_frozen(Path(args.cases))
    print(json.dumps({"problems": problems, "intact": not problems}, indent=2))
    return 1 if problems else 0


def _git_head() -> str:
    import subprocess
    r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                       check=False, cwd=Path(__file__).resolve().parents[2])
    return r.stdout.strip() or "unknown"


def cmd_run(args: argparse.Namespace) -> int:
    """Run a baseline through the harness on a selection of cases.

    The scripted upstream proves plumbing only; its report says so and is never a result.
    The OpenAI upstream reads its key from OPENAI_API_KEY and never writes it anywhere.
    A run on the test split needs a stated purpose and is appended to TEST_RUNS.log."""
    import datetime as dt

    from eeb.baselines.agents import make_app
    from eeb.harness.run import AppServer, RunConfig, run
    from eeb.harness.upstreams import OpenAICompatibleUpstream, ScriptedUpstream, Upstream

    cases = [c for c in read_jsonl(Path(args.cases)) if c["split"] == args.split
             and (not args.classes or c["class"] in args.classes.split(","))]
    if args.limit:
        cases = cases[:args.limit]
    if args.split == "test" and not args.purpose:
        sys.exit("a test-split run needs --purpose (spec §11: test runs are logged)")
    upstream: Upstream
    if args.upstream == "scripted":
        upstream = ScriptedUpstream()
    else:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            sys.exit("OPENAI_API_KEY is not set")
        upstream = OpenAICompatibleUpstream(base_url=args.base_url, api_key=key, name="openai")
    container = args.pg_container or os.environ.get("EEB_PG_CONTAINER")
    if not container:
        sys.exit("the Postgres container is required for the statement log (EEB_PG_CONTAINER)")
    with AppServer(make_app(args.baseline)) as sut:
        report = run(RunConfig(instance=Path(args.instance), cases=cases, sut_url=sut.url,
                               mode="S", admin_dsn=_dsn(args.pg_dsn), pg_container=container,
                               out_dir=Path(args.out), upstream=upstream,
                               model_id=args.model_id, sut_name=args.baseline,
                               model_access_mode="gateway_only"))
    report["plumbing_only"] = args.upstream == "scripted"
    (Path(args.out) / "REPORT.json").write_text(json.dumps(report, indent=2, sort_keys=True,
                                                           default=str) + "\n", "utf-8")
    if args.split == "test":
        line = {"timestamp": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                "commit": _git_head(), "baseline": args.baseline, "model_id": args.model_id,
                "upstream": args.upstream, "cases": len(cases), "classes": args.classes,
                "purpose": args.purpose, "out": Path(args.out).name}
        with Path(args.test_runs_log).open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
    o = report["scores"]["overall"]
    print(json.dumps({"cases": report["cases"], "plumbing_only": report["plumbing_only"],
                      "context_exposure": report["context_exposure"]["verdict"],
                      "false_answer": o["false_answer"], "fact_recall": o["fact_recall"]},
                     indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eeb")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="generate and validate an instance")
    b.add_argument("--seed", type=int, required=True)
    b.add_argument("--scale", choices=sorted(SCALES), default="small")
    b.add_argument("--out", required=True)
    b.add_argument("--pg-dsn")
    b.add_argument("--keep-db", action="store_true")
    b.set_defaults(func=cmd_build)
    v = sub.add_parser("verify", help="rebuild a fresh database from an instance and re-check")
    v.add_argument("--instance", required=True)
    v.add_argument("--pg-dsn")
    v.set_defaults(func=cmd_verify)
    c = sub.add_parser("cases", help="build or verify the case corpus of an instance")
    csub = c.add_subparsers(dest="cases_cmd", required=True)
    cb = csub.add_parser("build", help="bind the frozen plan to validated cases")
    cb.add_argument("--instance", required=True)
    cb.add_argument("--out", required=True)
    cb.add_argument("--pg-dsn", help="also re-execute every SQL gold under its principal's "
                                     "login (default: EEB_PG_ADMIN_DSN when set)")
    cb.set_defaults(func=cmd_cases_build)
    cv = csub.add_parser("verify", help="rebuild from the instance and compare")
    cv.add_argument("--cases", required=True)
    cv.add_argument("--instance", required=True)
    cv.add_argument("--reference-family",
                    help="model family of the reference agent; paraphrases from it are "
                         "rejected")
    cv.set_defaults(func=cmd_cases_verify)
    cr = csub.add_parser("rephrase", help="paraphrase the queue with an independent family")
    cr.add_argument("--cases", required=True)
    cr.add_argument("--model", required=True, help="exact model id, recorded per entry")
    cr.add_argument("--reference-family", default="openai")
    cr.add_argument("--only", choices=["pending", "rejected"], default="pending")
    cr.add_argument("--limit", type=int, default=0, help="at most this many (a pilot)")
    cr.set_defaults(func=cmd_cases_rephrase)
    cf = csub.add_parser("freeze", help="freeze a verified, reviewed and paraphrased corpus")
    cf.add_argument("--cases", required=True)
    cf.add_argument("--instance", required=True)
    cf.add_argument("--reference-family", default="openai")
    cf.add_argument("--spec", help="the frozen spec (default: docs/spec.md)")
    cf.set_defaults(func=cmd_cases_freeze)
    cvf = csub.add_parser("verify-frozen", help="check a frozen corpus is unchanged")
    cvf.add_argument("--cases", required=True)
    cvf.set_defaults(func=cmd_cases_verify_frozen)
    r = sub.add_parser("run", help="run a baseline through the harness")
    r.add_argument("baseline", choices=["b1", "b2", "b3"])
    r.add_argument("--instance", required=True)
    r.add_argument("--cases", required=True, help="cases.jsonl of the corpus")
    r.add_argument("--split", choices=["dev", "test"], default="dev")
    r.add_argument("--classes", help="comma-separated case classes (default: all)")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--upstream", choices=["scripted", "openai"], default="scripted")
    r.add_argument("--model-id", default="scripted")
    r.add_argument("--base-url", default="https://api.openai.com/v1")
    r.add_argument("--out", required=True)
    r.add_argument("--purpose", help="required for the test split")
    r.add_argument("--test-runs-log", default="TEST_RUNS.log")
    r.add_argument("--pg-dsn")
    r.add_argument("--pg-container")
    r.set_defaults(func=cmd_run)
    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
