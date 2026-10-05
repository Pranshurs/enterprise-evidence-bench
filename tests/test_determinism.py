"""Generator determinism is a hard property (G1)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from eeb.generator.core import Config
from eeb.generator.instance import build_instance_files

# Recorded once; every supported Python version must reproduce it (run on the 3.11-3.14
# matrix). Any intended generator change must update it together with GENERATOR_VERSION.
GOLDEN_SMALL_SEED7 = "79d8c4d13dca4f2c011310172ca8c9082da497087499e78f1b65543a332a76ca"


def test_same_seed_gives_byte_identical_artifacts(files: dict[str, bytes]) -> None:
    again = build_instance_files(Config(seed=7))
    assert again.keys() == files.keys()
    differing = [k for k in files if files[k] != again[k]]
    assert differing == []


def test_every_artifact_family_is_covered_by_the_instance_digest(files: dict[str, bytes]) -> None:
    meta = json.loads(files["INSTANCE.json"])
    covered = set(meta["files"])
    for family in ("tables/", "docs/", "manifest/documents.jsonl", "principals.jsonl",
                   "assignments.jsonl", "registry/canaries.jsonl",
                   "registry/sensitive_values.jsonl", "gold/scenario_facts.jsonl",
                   "cases/plan.jsonl", "authorization/outcome.json", "policy.yaml"):
        assert any(p.startswith(family) for p in covered), family
    assert covered == set(files) - {"INSTANCE.json"}


def test_golden_digest(files: dict[str, bytes]) -> None:
    assert json.loads(files["INSTANCE.json"])["instance_digest"] == GOLDEN_SMALL_SEED7


def test_different_seed_changes_the_instance(files: dict[str, bytes]) -> None:
    other = build_instance_files(Config(seed=8))
    assert json.loads(other["INSTANCE.json"])["instance_digest"] != json.loads(
        files["INSTANCE.json"])["instance_digest"]
    assert other["policy.yaml"] == files["policy.yaml"]


def test_no_wall_clock_paths_or_hash_seed_dependence(tmp_path: Path) -> None:
    code = ("import json; from eeb.generator.core import Config; "
            "from eeb.generator.instance import build_instance_files; "
            "print(json.loads(build_instance_files(Config(seed=7))['INSTANCE.json'])"
            "['instance_digest'])")
    digests = set()
    for tz, hashseed, cwd in (("UTC", "0", tmp_path), ("Asia/Kolkata", "12345", Path("/")),
                              ("America/Los_Angeles", "random", tmp_path)):
        env = {**os.environ, "TZ": tz, "PYTHONHASHSEED": hashseed, "LC_ALL": "C"}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             check=True, env=env, cwd=cwd)
        digests.add(out.stdout.strip())
    assert digests == {GOLDEN_SMALL_SEED7}


def test_no_artifact_contains_a_local_path(files: dict[str, bytes]) -> None:
    needles = [str(Path.home()).encode(), b"/private/tmp", b"/Users/", b"/home/"]
    assert [k for k, v in files.items() if any(n in v for n in needles)] == []


@pytest.mark.slow
def test_default_scale_is_deterministic_and_large_enough() -> None:
    a = build_instance_files(Config(seed=11, scale="default"))
    b = build_instance_files(Config(seed=11, scale="default"))
    assert [k for k in a if a[k] != b[k]] == []
    assert json.loads(a["INSTANCE.json"])["counts"]["po_lines"] >= 100_000
