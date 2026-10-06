"""The run loop: drive a SUT through the §8 contract and score what the harness observed.

One run binds one SUT, one credential mode and one set of cases to a fresh database
namespace and a fresh recording gateway. Cases are asked strictly one at a time; each is
bracketed by a gateway window and database log markers, so model calls and statements are
attributed to the request that caused them (ADR-0005, ADR-0006). After the last case the
harness reads the statement log, re-executes every receipt under the asking principal's
verifier twin, assesses context exposure from the gateway log, and scores.

This module runs the gateway on localhost in the same process: isolation is *unenforced*
(context exposure can be LOWER_BOUND or UNOBSERVED, never a complete 0). Isolated runs
use ``harness/isolation`` instead.

SUT setup (``POST {sut}/v1/setup``) receives:
``{"instance_dir", "documents_dir", "metrics_yaml", "gateway_url", "mode",
  "credentials": {ref: dsn}}``. Requests name their credential by ``db_credential_ref``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from eeb.cases.view import InstanceData
from eeb.db.load import build_database, drop, dsn_for
from eeb.exposure import from_instance_dir
from eeb.harness import dbobserve
from eeb.harness.gateway import Gateway, new_request_id
from eeb.harness.observation import assess
from eeb.harness.receipts import verify_receipt
from eeb.harness.recorder import read_log
from eeb.harness.upstreams import Upstream
from eeb.policy import sqlgen
from eeb.scoring import aggregate
from eeb.scoring.evidence import Evidence
from eeb.scoring.score import Observed, ReceiptVerdict, score_case

MODES = ("P", "S")


@dataclass
class RunConfig:
    instance: Path
    cases: list[dict[str, Any]]
    sut_url: str
    mode: str
    admin_dsn: str
    pg_container: str
    out_dir: Path
    upstream: Upstream
    model_id: str = "scripted"
    model_access_mode: str = "undeclared"
    sut_name: str = "sut"
    timeout_s: float = 300.0
    ns: str = field(default_factory=lambda: "eebr_" + new_request_id()[:12])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class AppServer:
    """An ASGI app (the gateway, or a SUT in tests) served on localhost from a background
    thread."""

    def __init__(self, app: Any) -> None:
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
                                                    port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> AppServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("gateway did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def credentials(cfg: RunConfig, principals: list[str]) -> dict[str, str]:
    """Mode P: one login per principal. Mode S: the read-only service login only."""
    if cfg.mode == "P":
        return {f"principal:{p}": dsn_for(cfg.admin_dsn, cfg.ns, sqlgen.login_role(cfg.ns, p),
                                          sqlgen.login_password(cfg.ns, p))
                for p in principals}
    return {"service": dsn_for(cfg.admin_dsn, cfg.ns, sqlgen.service_role(cfg.ns),
                               sqlgen.service_password(cfg.ns))}


def run(cfg: RunConfig) -> dict[str, Any]:
    if cfg.mode not in MODES:
        raise ValueError(cfg.mode)
    cfg.out_dir.mkdir(parents=True, exist_ok=False)
    data = InstanceData.load(cfg.instance)
    principals = sorted(p["principal_id"] for p in data.principals)
    build_database(cfg.admin_dsn, cfg.instance, cfg.ns)
    try:
        return _run(cfg, data, principals)
    finally:
        drop(cfg.admin_dsn, cfg.ns)


def _run(cfg: RunConfig, data: InstanceData, principals: list[str]) -> dict[str, Any]:
    gateway = Gateway(cfg.out_dir / "gateway.jsonl", cfg.upstream)
    attrs = {p["principal_id"]: {k: v for k, v in p.items() if k != "principal_id"}
             for p in data.principals}
    responses: list[dict[str, Any]] = []
    with AppServer(gateway.app) as gw, httpx.Client(timeout=cfg.timeout_s) as client:
        setup = {"instance_dir": str(cfg.instance), "documents_dir": str(cfg.instance / "docs"),
                 "metrics_yaml": str(resources.files("eeb").joinpath("data/metrics.yaml")),
                 "gateway_url": gw.url, "model_id": cfg.model_id,
                 "mode": cfg.mode, "credentials": credentials(cfg, principals)}
        # Setup gets its own marked window: what the SUT runs while preparing (schema
        # introspection, ingest) is attributed to setup, not left unattributed.
        setup_rid = new_request_id()
        dbobserve.mark(cfg.admin_dsn, cfg.ns, "open", setup_rid)
        try:
            r = client.post(f"{cfg.sut_url}/v1/setup", json=setup)
        finally:
            dbobserve.mark(cfg.admin_dsn, cfg.ns, "close", setup_rid)
        r.raise_for_status()
        for case in cfg.cases:
            rid = new_request_id()
            pid = case["principal_id"]
            gateway.open_window(rid, "request", pid, case["question"])
            dbobserve.mark(cfg.admin_dsn, cfg.ns, "open", rid)
            ask = {"request_id": rid, "question": case["question"], "as_of": case["as_of"],
                   "principal": {"id": pid, "attributes": attrs.get(pid, {}),
                                 "db_credential_ref": (f"principal:{pid}" if cfg.mode == "P"
                                                       else "service")}}
            t0 = time.monotonic()
            try:
                resp = client.post(f"{cfg.sut_url}/v1/ask", json=ask)
                body: Any = resp.json() if resp.status_code == 200 else None
                error = None if resp.status_code == 200 else f"http_{resp.status_code}"
            except (httpx.HTTPError, ValueError) as e:
                body, error = None, type(e).__name__
            latency = round((time.monotonic() - t0) * 1000, 3)
            dbobserve.mark(cfg.admin_dsn, cfg.ns, "close", rid)
            gateway.close_window(rid)
            responses.append({"case_id": case["case_id"], "request_id": rid,
                              "latency_ms": latency, "error": error, "response": body})
    return _score(cfg, data, responses, setup_rid)


def _score(cfg: RunConfig, data: InstanceData, responses: list[dict[str, Any]],
           setup_rid: str) -> dict[str, Any]:
    by_case = {c["case_id"]: c for c in cfg.cases}
    logins = {sqlgen.login_role(cfg.ns, p["principal_id"]) for p in data.principals} | {
        sqlgen.service_role(cfg.ns)}
    admin_user = cfg.admin_dsn.split("://", 1)[1].split(":", 1)[0]
    per, unattributed, anomalies = dbobserve.attribute(
        dbobserve.read_container_log(cfg.pg_container), cfg.ns, logins, admin_user)
    assert data.oracle is not None
    scanner = from_instance_dir(cfg.instance, data.oracle, data.tables)
    records = read_log(cfg.out_dir / "gateway.jsonl")
    exposure = assess(records, scanner, "unenforced", cfg.model_access_mode,
                      {r["request_id"]: by_case[r["case_id"]]["question"] for r in responses})
    canaries = _canaries(cfg.instance)
    ev = Evidence(data)
    scores = []
    for r in responses:
        case = by_case[r["case_id"]]
        stmts = per.get(r["request_id"], [])
        obs = Observed(statements=[{"sql": s.sql, "outcome": s.outcome, "harmful": s.harmful}
                                   for s in stmts], latency_ms=r["latency_ms"])
        body = r["response"]
        if isinstance(body, dict):
            for rec in body.get("sql_receipts", []) or []:
                if isinstance(rec, dict) and isinstance(rec.get("receipt_id"), str):
                    chk = verify_receipt(cfg.admin_dsn, cfg.ns, case["principal_id"], rec,
                                         cfg.mode, stmts)
                    obs.receipts[rec["receipt_id"]] = ReceiptVerdict(chk.status, chk.columns,
                                                                     chk.rows)
        s = score_case(case, body, ev, obs, scanner, canaries)
        s["transport_error"] = r["error"]
        s["receipt_status"] = {k: v.status for k, v in sorted(obs.receipts.items())}
        s["sql_summary"] = dbobserve.summarize(stmts)
        scores.append(s)
    report = {
        "sut": cfg.sut_name, "mode": cfg.mode, "cases": len(cfg.cases),
        "model_access_mode": cfg.model_access_mode, "upstream": cfg.upstream.name,
        "isolation": "unenforced",
        "context_exposure": {"verdict": exposure.verdict,
                             "exposure_count": exposure.exposure_count,
                             "observed_event_count": exposure.observed_event_count,
                             "chain_problems": exposure.chain_problems,
                             "anomalies": len(exposure.anomalies)},
        "statements": {"unattributed": len(unattributed), "anomalies": anomalies,
                       "setup": dbobserve.summarize(per.get(setup_rid, []))},
        "scores": aggregate.report(scores),
    }
    # Run records are not canonical artifacts (latencies and rates are floats; responses
    # are whatever the SUT sent): sorted keys, one record per line.
    for name, recs in (("responses.jsonl", responses), ("scores.jsonl", scores)):
        (cfg.out_dir / name).write_text("".join(
            json.dumps(x, sort_keys=True, default=str) + "\n" for x in recs), "utf-8")
    (cfg.out_dir / "REPORT.json").write_text(json.dumps(report, indent=2, sort_keys=True,
                                                         default=str) + "\n", "utf-8")
    return report


def _canaries(instance: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for line in (instance / "registry/canaries.jsonl").read_text("utf-8").splitlines():
        if line:
            rec = json.loads(line)
            # Row and cell canaries name their table; chunk canaries live in doc_chunks.
            out.setdefault(rec["location"].get("table", "doc_chunks"), set()).add(rec["canary"])
    return out
