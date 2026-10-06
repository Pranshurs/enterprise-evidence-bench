"""Command line: ``eeb build``, ``eeb verify`` and ``eeb cases``.

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
from eeb.db.load import build_database, drop, export_tables, read_instance
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
    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
