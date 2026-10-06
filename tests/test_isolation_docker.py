"""End-to-end observation boundary under Docker (ADR-0005 §6-7).

The enforced runs prove that bypass is impossible and that what reaches the model is
recorded. The open-network runs are the negative control: the probe must notice the open
egress, and a partial bypass must then be reported UNOBSERVED (or LOWER_BOUND), never
clean."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eeb.exposure import ExposureScanner, from_instance_dir
from eeb.harness.gateway import new_request_id
from eeb.harness.isolation import IsolatedRun, build_gateway_image
from eeb.harness.observation import assess
from eeb.harness.recorder import read_log
from eeb.policy.oracle import Oracle

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.docker
Rows = dict[str, list[dict[str, Any]]]


def _docker_ok() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="module", autouse=True)
def gateway_image() -> None:
    if not _docker_ok():
        if os.environ.get("EEB_REQUIRE_DOCKER") == "1":
            pytest.fail("EEB_REQUIRE_DOCKER=1 but docker is unavailable")
        pytest.skip("docker unavailable")
    build_gateway_image(ROOT)


@pytest.fixture(scope="module")
def scanner(instance_dir: Path, oracle: Oracle, tables: Rows) -> ExposureScanner:
    return from_instance_dir(instance_dir, oracle, tables)


def _leak(tables: Rows) -> str:
    s = next(x for x in tables["suppliers"] if x["category_id"] == "CAT-ELC")
    return f"{s['name']} {s['supplier_id']} {s['row_tag']}"


def _run(mode: str, internal: bool, context: str, tmp: str,
         declare: str = "gateway_only") -> tuple[IsolatedRun, Any, Path]:
    workdir = ROOT / ".runs" / f"{tmp}-{new_request_id()[:8]}"
    run = IsolatedRun(ROOT, workdir, {"SUT_MODE": mode, "SUT_DECLARE": declare},
                      internal=internal)
    with run:
        run.probe()
        run.declared = run.declared_model_access()  # type: ignore[attr-defined]
        rid = new_request_id()
        reply = run.ask(rid, "cm_met", "What happened last quarter?", {"context": context})
        run.probe()
    return run, reply, workdir


def test_enforced_isolation_observes_a_leak(scanner: ExposureScanner, tables: Rows) -> None:
    run, reply, wd = _run("compliant", True, _leak(tables), "enf-leak")
    try:
        assert run.isolation == "enforced", run.probes
        rep = assess(read_log(wd / "gateway.jsonl"), scanner, run.isolation,
                     run.declared)  # type: ignore[attr-defined]
        assert rep.verdict == "OBSERVED" and rep.exposure_count and rep.exposure_count >= 3
        assert {e.kind for r in rep.per_request for e in r.exposures} == {
            "name", "identifier", "canary"}
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def test_enforced_isolation_blocks_bypass(scanner: ExposureScanner, tables: Rows) -> None:
    run, reply, wd = _run("bypass", True, _leak(tables), "enf-bypass")
    try:
        assert run.isolation == "enforced"
        assert reply["body"]["notes"] and reply["body"]["notes"][0].startswith(
            "bypass:egress_blocked")
        rep = assess(read_log(wd / "gateway.jsonl"), scanner, run.isolation,
                     run.declared)  # type: ignore[attr-defined]
        assert rep.verdict == "OBSERVED" and rep.exposure_count == 0
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def test_open_network_is_detected_and_bypass_is_never_reported_clean(
        scanner: ExposureScanner, tables: Rows) -> None:
    run, reply, wd = _run("bypass", False, _leak(tables), "open-bypass")
    try:
        assert run.isolation == "unenforced"
        assert any(p["external_ip_reachable"] or p["default_route_present"] for p in run.probes)
        assert reply["body"]["notes"] == ["bypass:egress_open"]
        rep = assess(read_log(wd / "gateway.jsonl"), scanner, run.isolation,
                     run.declared)  # type: ignore[attr-defined]
        assert rep.verdict == "UNOBSERVED" and rep.exposure_count is None
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def test_open_network_leak_through_gateway_is_a_lower_bound(scanner: ExposureScanner,
                                                            tables: Rows) -> None:
    run, reply, wd = _run("compliant", False, _leak(tables), "open-leak")
    try:
        assert run.isolation == "unenforced"
        rep = assess(read_log(wd / "gateway.jsonl"), scanner, run.isolation,
                     run.declared)  # type: ignore[attr-defined]
        assert rep.verdict == "LOWER_BOUND" and rep.exposure_count and rep.exposure_count >= 3
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ---------------------------------------------------------------- in-process inference
def test_in_process_inference_cannot_be_observed(scanner: ExposureScanner, tables: Rows) -> None:
    """ADR-0005 limitation, demonstrated. The SUT feeds restricted context to an in-process
    "model" and sends only the question through the gateway. Honest declarations
    (mixed/embedded) and a missing declaration all yield UNOBSERVED, never zero."""
    leak = _leak(tables)
    for declare in ("mixed", "embedded", "none"):
        run, reply, wd = _run("local", True, leak, f"local-{declare}", declare=declare)
        try:
            assert run.isolation == "enforced"
            assert reply["body"]["notes"][0].startswith("local-summary:")
            log_bytes = (wd / "gateway.jsonl").read_bytes()
            assert all(tok.encode() not in log_bytes for tok in leak.split()[-2:])
            rep = assess(read_log(wd / "gateway.jsonl"), scanner, run.isolation,
                         run.declared)  # type: ignore[attr-defined]
            expected_mode = declare if declare != "none" else "undeclared"
            assert rep.model_access_mode == expected_mode
            assert rep.verdict == "UNOBSERVED" and rep.exposure_count is None
        finally:
            shutil.rmtree(wd, ignore_errors=True)


def test_false_gateway_only_declaration_is_an_undetectable_limitation(
        scanner: ExposureScanner, tables: Rows) -> None:
    """The same in-process SUT *falsely* declares gateway_only. The harness has no evidence
    to the contrary: the verdict is OBSERVED/0, but only within observation_scope
    (externally mediated model context), and the restricted context it consumed locally
    is provably absent from everything the harness recorded. This is the stated limit of
    the observation boundary, not a coverage claim."""
    leak = _leak(tables)
    run, reply, wd = _run("local", True, leak, "local-lying", declare="gateway_only")
    try:
        rep = assess(read_log(wd / "gateway.jsonl"), scanner, run.isolation,
                     run.declared)  # type: ignore[attr-defined]
        assert rep.verdict == "OBSERVED" and rep.exposure_count == 0
        assert rep.observation_scope == "externally_mediated_model_context"
        assert reply["body"]["notes"][0].startswith("local-summary:")
        assert all(tok.encode() not in (wd / "gateway.jsonl").read_bytes()
                   for tok in leak.split()[-2:])
    finally:
        shutil.rmtree(wd, ignore_errors=True)
