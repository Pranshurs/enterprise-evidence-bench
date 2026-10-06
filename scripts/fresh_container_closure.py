"""Two-fresh-container substrate closure.

Builds the same instance with ``eeb build`` on two brand-new ``postgres:17`` containers
(JSON statement logging enabled, as required by ADR-0006). The first container is
destroyed before the second starts. The script then compares every artifact byte and the
full validation report.

The validation report covers:
- reload byte-identity;
- oracle/RLS agreement;
- verifier-twin equivalence;
- Mode-S service visibility;
- hardening and logging configuration;
- gold SQL answers.

It also cross-verifies instance A on container B. The record contains hashes and counts
only; no raw database logs.

    python scripts/fresh_container_closure.py --seed 7 --scale default --out record.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PORT = "55433"
DSN = f"postgresql://eebadmin:eebadmin@127.0.0.1:{PORT}/postgres"
PG_ARGS = ["-c", "logging_collector=on", "-c", "log_destination=jsonlog",
           "-c", "log_directory=log", "-c", "log_filename=postgresql.log"]


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def fresh(name: str) -> str:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    cid = sh("docker", "run", "-d", "--name", name, "-e", "POSTGRES_USER=eebadmin",
             "-e", "POSTGRES_PASSWORD=eebadmin", "-p", f"127.0.0.1:{PORT}:5432", "postgres:17",
             *PG_ARGS)
    for _ in range(60):
        if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "eebadmin", "-q"]
                          ).returncode == 0:
            time.sleep(2)
            return cid[:12]
        time.sleep(1)
    raise RuntimeError("container not ready")


def tree(p: Path) -> dict[str, str]:
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(p.rglob("*")) if f.is_file()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scale", default="default")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    eeb = str(Path(sys.executable).with_name("eeb"))
    env = {**os.environ, "EEB_PG_ADMIN_DSN": DSN}
    work = Path(tempfile.mkdtemp(prefix="eeb-closure-"))
    containers = {}
    try:
        for label in ("A", "B"):
            containers[label] = fresh("eeb-closure")
            subprocess.run([eeb, "build", "--seed", str(args.seed), "--scale", args.scale,
                            "--out", str(work / label)], env=env, check=True,
                           capture_output=True)
            if label == "A":
                continue
            verify = subprocess.run([eeb, "verify", "--instance", str(work / "A")], env=env,
                                    capture_output=True, text=True)
            (work / "A_verify_on_B.json").write_text(verify.stdout)
    finally:
        subprocess.run(["docker", "rm", "-f", "eeb-closure"], capture_output=True)
    a, b = tree(work / "A"), tree(work / "B")
    va = json.loads((work / "A" / "VALIDATION.json").read_text())
    vv = json.loads((work / "A_verify_on_B.json").read_text())

    def pick(v: dict[str, object]) -> dict[str, object]:
        keys = ("passed", "database_reload_identical", "authorization_outcome_digest",
                "service_outcome_digest", "gold_sql")
        return {k: v[k] for k in keys} | {
            k: len(v[k]) for k in ("disagreements", "hardening_problems",  # type: ignore[arg-type]
                                   "twin_disagreements", "service_problems")}

    record = {
        "procedure": (f"eeb build --seed {args.seed} --scale {args.scale} on fresh postgres:17 "
                      "container A (JSON statement logging); container destroyed; same build on "
                      "fresh container B; eeb verify of instance A on container B"),
        "containers": containers, "instance_digest": va["instance_digest"],
        "files_compared": len(a), "byte_identical_A_vs_B": a == b,
        "validation_A": pick(va), "verify_A_on_B": pick(vv),
    }
    Path(args.out).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    shutil.rmtree(work)
    return 0 if record["byte_identical_A_vs_B"] and va["passed"] and vv["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
