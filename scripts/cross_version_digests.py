"""Cross-interpreter byte-identity gate for the generator.

Runs the generator under each given Python interpreter (each must have this package
installed) and compares the per-file SHA-256 map of every emitted artifact. This compares
the bytes the interpreters actually emit, which is stronger than a test suite being green on
each version.

    python scripts/cross_version_digests.py --python /path/py311 /path/py312 ... \
        --case 7:small --case 1234:small --case 7:default --out report.json

Exit status 0 only if every interpreter emits identical digests for every case.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

PROBE = """
import json, sys
from eeb.generator.core import Config
from eeb.generator.instance import build_instance_files
from eeb import canonical
seed, scale = int(sys.argv[1]), sys.argv[2]
files = build_instance_files(Config(seed=seed, scale=scale))
meta = json.loads(files["INSTANCE.json"])
print(json.dumps({
    "python": sys.version.split()[0],
    "instance_digest": meta["instance_digest"],
    "instance_json_sha256": canonical.sha256_bytes(files["INSTANCE.json"]),
    "files": {k: canonical.sha256_bytes(v) for k, v in sorted(files.items())},
}))
"""


def differing_files(runs: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Per interpreter (after the first), the artifact paths whose digest differs."""
    reference = runs[0]["files"]
    return {r["python"]: sorted(k for k in set(reference) | set(r["files"])
                                if reference.get(k) != r["files"].get(k)) for r in runs[1:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", nargs="+", required=True)
    ap.add_argument("--case", action="append", required=True, help="seed:scale")
    ap.add_argument("--out")
    args = ap.parse_args()
    report: dict[str, object] = {"cases": {}}
    ok = True
    for case in args.case:
        seed, scale = case.split(":")
        runs = []
        for py in args.python:
            out = subprocess.run([py, "-c", PROBE, seed, scale], capture_output=True, text=True,
                                 check=True)
            runs.append(json.loads(out.stdout))
        diffs = differing_files(runs)
        identical = all(not d for d in diffs.values())
        ok &= identical
        report["cases"][case] = {  # type: ignore[index]
            "interpreters": [r["python"] for r in runs],
            "instance_digest": sorted({r["instance_digest"] for r in runs}),
            "file_count": len(runs[0]["files"]),
            "identical": identical,
            "differing_files": diffs,
        }
    report["all_identical"] = ok
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
