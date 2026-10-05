"""Command line: ``eeb build`` and ``eeb verify``.

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

from eeb import canonical
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
                                         "database": result.db_digest},
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
    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
