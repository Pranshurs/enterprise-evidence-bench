"""Context-exposure assessment from the gateway log (spec §10.3; ADR-0005, ACCEPTED).

Scope: the verdict covers **externally mediated model context only**, meaning text that
reached a model through the harness gateway. Network isolation proves the absence of
ordinary network egress paths that the harness covers. It does not prove the absence of
inference running wholly inside the SUT's process or container. The SUT's model-access
declaration defines applicability; it is not evidence of coverage.

Headline verdict, applied in order:
- ``INVALID``: the gateway log's hash chain is broken.
- ``UNOBSERVED``: ``model_access_mode`` is not ``gateway_only`` (``embedded``, ``mixed``
  or ``undeclared``), or the harness found evidence of an unobserved inference path.
  Gateway-observed exposures are still reported as observed *events*, never as a complete
  count or as zero.
- ``OBSERVED``: the harness-enforced gateway-only profile, with isolation proven by
  active probes. The count is complete *within* ``observation_scope``.
- ``LOWER_BOUND``: gateway-only declared, isolation not enforced, ≥ 1 exposure seen.
- ``UNOBSERVED``: gateway-only declared, isolation not enforced, 0 exposures seen.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from eeb.exposure import Exposure, ExposureScanner
from eeb.harness.recorder import verify_chain
from eeb.harness.text import model_input_text

MODEL_ACCESS_MODES = ("gateway_only", "embedded", "mixed", "undeclared")
OBSERVATION_SCOPE = "externally_mediated_model_context"
NOBODY = "nobody"  # a principal with no access: everything restricted counts


@dataclass
class RequestExposure:
    request_id: str
    principal_id: str | None
    calls: int
    exposures: list[Exposure]


@dataclass
class ContextExposureReport:
    verdict: str
    # Complete (OBSERVED) or "at least" (LOWER_BOUND) count; None otherwise.
    exposure_count: int | None
    # Exposures the gateway saw, always reported; never a completeness claim by itself.
    observed_event_count: int = 0
    observation_scope: str = OBSERVATION_SCOPE
    model_access_mode: str = "undeclared"
    unobserved_path_evidence: list[str] = field(default_factory=list)
    per_request: list[RequestExposure] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    rejected_with_restricted_content: int = 0
    ingest_restricted_egress: int = 0
    chain_problems: list[str] = field(default_factory=list)
    isolation: str = "unenforced"


def assess(records: list[dict[str, Any]], scanner: ExposureScanner, isolation: str,
           model_access_mode: str = "undeclared",
           questions: Mapping[str, str] | None = None,
           unobserved_path_evidence: Sequence[str] = ()) -> ContextExposureReport:
    if isolation not in ("enforced", "unenforced"):
        raise ValueError(isolation)
    if model_access_mode not in MODEL_ACCESS_MODES:
        raise ValueError(model_access_mode)
    questions = questions or {}
    report = ContextExposureReport(verdict="", exposure_count=None, isolation=isolation,
                                   model_access_mode=model_access_mode,
                                   unobserved_path_evidence=list(unobserved_path_evidence))
    report.chain_problems = verify_chain(records)
    windows = {r["request_id"]: r for r in records if r["type"] == "window_open"}
    by_request: dict[str, RequestExposure] = {}
    for r in records:
        if r["type"] == "anomaly":
            report.anomalies.append({k: r[k] for k in ("reason", "path", "path_request_id",
                                                         "header_request_id")})
            try:
                text = model_input_text(json.loads(r["body_text"]))
            except ValueError:
                text = r["body_text"]
            if scanner.scan(NOBODY, text):
                report.rejected_with_restricted_content += 1
        elif r["type"] == "model_call":
            text = model_input_text(json.loads(r["body_text"]))
            w = windows.get(r["request_id"], {})
            if w.get("kind") == "ingest":
                report.ingest_restricted_egress += len(scanner.scan(NOBODY, text))
                continue
            pid = w.get("principal_id")
            supplied = [questions[r["request_id"]]] if r["request_id"] in questions else []
            found = scanner.scan(pid or NOBODY, text, supplied=supplied)
            entry = by_request.setdefault(r["request_id"],
                                          RequestExposure(r["request_id"], pid, 0, []))
            entry.calls += 1
            entry.exposures.extend(found)
    report.per_request = sorted(by_request.values(), key=lambda e: e.request_id)
    observed = sum(len(e.exposures) for e in report.per_request)
    report.observed_event_count = observed
    if report.chain_problems:
        report.verdict = "INVALID"
    elif model_access_mode != "gateway_only" or report.unobserved_path_evidence:
        report.verdict = "UNOBSERVED"
    elif isolation == "enforced":
        report.verdict, report.exposure_count = "OBSERVED", observed
    elif observed > 0:
        report.verdict, report.exposure_count = "LOWER_BOUND", observed
    else:
        report.verdict = "UNOBSERVED"
    return report
