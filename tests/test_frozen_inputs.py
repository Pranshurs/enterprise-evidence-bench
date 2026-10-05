"""The frozen spec stays byte-identical, and Avadhika stays out of this package."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN_SPEC_SHA256 = "3419920460863060ce53cfcfbc9750ecda341a7fc8da96279535fc9b02229e4b"


def test_frozen_spec_is_byte_identical() -> None:
    data = (ROOT / "docs" / "spec.md").read_bytes()
    assert hashlib.sha256(data).hexdigest() == FROZEN_SPEC_SHA256


def test_spec_checksum_test_can_fail(tmp_path: Path) -> None:
    mutated = (ROOT / "docs" / "spec.md").read_bytes() + b" "
    assert hashlib.sha256(mutated).hexdigest() != FROZEN_SPEC_SHA256


def test_avadhika_is_not_a_dependency() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    deps = meta["project"]["dependencies"] + sum(
        meta["project"].get("optional-dependencies", {}).values(), [])
    assert not any("avadhika" in d.lower() for d in deps)


def test_importing_every_module_never_loads_avadhika() -> None:
    code = (
        "import importlib, pkgutil, sys, eeb\n"
        "for m in pkgutil.walk_packages(eeb.__path__, 'eeb.'):\n"
        "    importlib.import_module(m.name)\n"
        "assert not any(k == 'avadhika' or k.startswith('avadhika.') for k in sys.modules)\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"


def test_no_source_mentions_avadhika() -> None:
    hits = [p for p in (ROOT / "src").rglob("*.py") if "avadhika" in p.read_text("utf-8").lower()]
    assert hits == []
