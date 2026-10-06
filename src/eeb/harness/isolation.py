"""Docker isolation runner (ADR-0005 §7).

Topology per run:
- ``<run>-int``: an ``internal`` network holding the SUT, the gateway (alias ``gateway``)
  and the benchmark database (alias ``db``);
- ``<run>-ctl``: an ordinary bridge holding only the gateway, published on 127.0.0.1 for
  the harness control plane and used for provider egress.

The SUT also gets an unroutable resolver, so external names do not resolve while
container aliases still do. A sidecar probe joins the SUT's network namespace and checks
reachability. That does not depend on anything inside the SUT image. A run counts as
``enforced`` only if the probes before and after the requests both pass.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

PROBE_IMAGE = "python:3.13-slim"
GATEWAY_IMAGE = "eeb-gateway:dev"
UNROUTABLE_DNS = "192.0.2.1"  # TEST-NET-1: resolver queries go nowhere


def docker(*args: str, check: bool = True) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and out.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def build_gateway_image(repo: Path) -> None:
    docker("build", "-q", "-f", str(repo / "docker/gateway.Dockerfile"), "-t", GATEWAY_IMAGE,
           str(repo))


@dataclass
class IsolatedRun:
    repo: Path
    workdir: Path  # must be inside a directory Docker can bind-mount (e.g. under $HOME)
    sut_env: dict[str, str]
    internal: bool = True
    run_id: str = field(default_factory=lambda: "eebr" + secrets.token_hex(4))
    secret: str = field(default_factory=lambda: secrets.token_hex(32))
    control_url: str = ""
    probes: list[dict[str, Any]] = field(default_factory=list)

    def _n(self, suffix: str) -> str:
        return f"{self.run_id}-{suffix}"

    def __enter__(self) -> IsolatedRun:
        self.workdir.mkdir(parents=True, exist_ok=True)
        internal = ["--internal"] if self.internal else []
        docker("network", "create", *internal, self._n("int"))
        docker("network", "create", self._n("ctl"))
        docker("run", "-d", "--name", self._n("gw"), "--network", self._n("ctl"),
               "-p", "127.0.0.1::8080", "-e", f"EEB_GATEWAY_CONTROL_SECRET={self.secret}",
               "-v", f"{self.workdir}:/logs", GATEWAY_IMAGE, "--log", "/logs/gateway.jsonl")
        docker("network", "connect", "--alias", "gateway", self._n("int"), self._n("gw"))
        docker("run", "-d", "--name", self._n("db"), "--network", self._n("int"),
               "--network-alias", "db", "-e", "POSTGRES_PASSWORD=probe-only", "postgres:17")
        env = [x for k, v in {"GATEWAY_URL": "http://gateway:8080", **self.sut_env}.items()
               for x in ("-e", f"{k}={v}")]
        docker("run", "-d", "--name", self._n("sut"), "--network", self._n("int"),
               "--network-alias", "sut", "--dns", UNROUTABLE_DNS, *env,
               "-v", f"{self.repo / 'docker/sut'}:/sut:ro", PROBE_IMAGE,
               "python", "/sut/test_sut.py")
        port = docker("port", self._n("gw"), "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
        self.control_url = f"http://127.0.0.1:{port}"
        self._wait()
        return self

    def _wait(self) -> None:
        for _ in range(60):
            try:
                httpx.post(f"{self.control_url}/_control/noop",
                           headers={"authorization": f"Bearer {self.secret}"}, json={},
                           timeout=2)
                probe = self.probe(record=False)
                if probe["gateway_reachable"] and probe["db_reachable"]:
                    return
            except (httpx.HTTPError, RuntimeError, KeyError):
                pass
            time.sleep(1)
        raise RuntimeError("isolated run did not become ready")

    def probe(self, record: bool = True) -> dict[str, Any]:
        out = docker("run", "--rm", "--network", f"container:{self._n('sut')}",
                     "-v", f"{self.repo / 'docker/probe.py'}:/probe.py:ro", PROBE_IMAGE,
                     "python", "/probe.py", "gateway", "db")
        result: dict[str, Any] = json.loads(out)
        if record:
            self.probes.append(result)
        return result

    @staticmethod
    def probe_enforced(p: dict[str, Any]) -> bool:
        return (not p["external_ip_reachable"] and not p["external_dns_resolves"]
                and not p["host_reachable"] and not p["default_route_present"]
                and p["gateway_reachable"] and p["db_reachable"])

    @property
    def isolation(self) -> str:
        return ("enforced" if len(self.probes) >= 2
                and all(self.probe_enforced(p) for p in self.probes) else "unenforced")

    def control(self, action: str, payload: dict[str, Any]) -> Any:
        r = httpx.post(f"{self.control_url}/_control/{action}", json=payload,
                       headers={"authorization": f"Bearer {self.secret}"}, timeout=300)
        r.raise_for_status()
        return r.json()

    def ask(self, request_id: str, principal_id: str, question: str,
            extra: dict[str, Any] | None = None) -> Any:
        self.control("open", {"request_id": request_id, "principal_id": principal_id,
                              "question": question})
        try:
            return self.control("ask", {"url": "http://sut:8000/v1/ask", "payload": {
                "request_id": request_id, "question": question,
                "principal": {"id": principal_id}, **(extra or {})}})
        finally:
            self.control("close", {"request_id": request_id})

    def __exit__(self, *exc: object) -> None:
        for c in ("sut", "db", "gw"):
            docker("rm", "-f", self._n(c), check=False)
        for n in ("int", "ctl"):
            docker("network", "rm", self._n(n), check=False)
