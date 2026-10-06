"""Gateway data plane, attribution, credential custody, log integrity and the three-state
verdict (in-process; Docker isolation is tested separately)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from eeb.exposure import ExposureScanner, from_instance_dir
from eeb.harness.gateway import Gateway, new_request_id
from eeb.harness.observation import assess
from eeb.harness.recorder import read_log, verify_chain
from eeb.harness.upstreams import OpenAICompatibleUpstream, ScriptedUpstream
from eeb.policy.oracle import Oracle

Rows = dict[str, list[dict[str, Any]]]


@pytest.fixture(scope="module")
def scanner(instance_dir: Path, oracle: Oracle, tables: Rows) -> ExposureScanner:
    return from_instance_dir(instance_dir, oracle, tables)


def call(gw: Gateway, path: str, body: Any, headers: dict[str, str] | None = None,
         raw: bytes | None = None) -> httpx.Response:
    async def go() -> httpx.Response:
        transport = httpx.ASGITransport(app=gw.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            content = raw if raw is not None else json.dumps(body).encode()
            return await c.post(path, content=content, headers=headers or {})
    return asyncio.run(go())


def chat(text: str, **extra: Any) -> dict[str, Any]:
    return {"model": "m", "messages": [{"role": "user", "content": text}], **extra}


@pytest.fixture()
def gw(tmp_path: Path) -> Gateway:
    return Gateway(tmp_path / "gateway.jsonl", ScriptedUpstream(), control_secret="s3cret")


def _open(gw: Gateway, pid: str = "cm_met", q: str = "q") -> str:
    rid = new_request_id()
    gw.open_window(rid, principal_id=pid, question=q)
    return rid


def test_compliant_call_is_recorded_and_answered(gw: Gateway) -> None:
    rid = _open(gw)
    r = call(gw, f"/r/{rid}/v1/chat/completions", chat("hello"), {"x-bench-request-id": rid})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"]
    recs = read_log(gw.recorder.path)
    assert verify_chain(recs) == []
    calls = [x for x in recs if x["type"] == "model_call"]
    assert len(calls) == 1 and json.loads(calls[0]["body_text"]) == chat("hello")
    assert any(x["type"] == "model_result" and x["reached_model"] for x in recs)


@pytest.mark.parametrize("case,reason,status", [
    ("missing_header", "missing_request_header", 400),
    ("mismatched_header", "header_path_mismatch", 400),
    ("unknown_id", "unknown_request_id", 403),
    ("closed", "window_closed", 403),
    ("non_json", "non_json_body", 400),
    ("stream", "streaming_unsupported_v1", 400),
    ("unknown_endpoint", "unknown_endpoint", 404),
    ("outside_r", "unknown_path", 404),
])
def test_rejections_are_recorded_and_never_reach_the_model(gw: Gateway, case: str, reason: str,
                                                           status: int) -> None:
    rid = _open(gw)
    other = new_request_id()
    path, headers, body, raw = f"/r/{rid}/v1/chat/completions", {"x-bench-request-id": rid}, \
        chat("x"), None
    if case == "missing_header":
        headers = {}
    elif case == "mismatched_header":
        headers = {"x-bench-request-id": other}
    elif case == "unknown_id":
        path, headers = f"/r/{other}/v1/chat/completions", {"x-bench-request-id": other}
    elif case == "closed":
        gw.close_window(rid)
    elif case == "non_json":
        raw = b"not json"
    elif case == "stream":
        body = chat("x", stream=True)
    elif case == "unknown_endpoint":
        path = f"/r/{rid}/v1/completions"
    elif case == "outside_r":
        path = "/v1/chat/completions"
    r = call(gw, path, body, headers, raw)
    assert r.status_code == status
    assert gw.upstream.calls == []  # type: ignore[attr-defined]
    recs = read_log(gw.recorder.path)
    assert [x["reason"] for x in recs if x["type"] == "anomaly"] == [reason]
    assert verify_chain(recs) == []


def test_control_plane_requires_the_secret(gw: Gateway) -> None:
    rid = new_request_id()
    r = call(gw, "/_control/open", {"request_id": rid}, {"authorization": "Bearer wrong"})
    assert r.status_code == 403 and rid not in gw.windows
    r = call(gw, "/_control/open", {"request_id": rid, "principal_id": "cm_met"},
             {"authorization": "Bearer s3cret"})
    assert r.status_code == 200 and gw.windows[rid].principal_id == "cm_met"


def test_strict_sequential_windows(gw: Gateway) -> None:
    _open(gw)
    with pytest.raises(RuntimeError):
        _open(gw)


def test_request_ids_are_single_use(gw: Gateway) -> None:
    rid = _open(gw)
    gw.close_window(rid)
    with pytest.raises(ValueError):
        gw.open_window(rid)


def test_sut_credentials_are_replaced_by_gateway_custody(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 3,
                                                                  "completion_tokens": 1}})

    up = OpenAICompatibleUpstream("https://provider.example/v1", "real-key",
                                  transport=httpx.MockTransport(handler))
    gw = Gateway(tmp_path / "g.jsonl", up)
    rid = new_request_id()
    gw.open_window(rid, principal_id="cm_met")
    call(gw, f"/r/{rid}/v1/chat/completions", chat("hi"),
         {"x-bench-request-id": rid, "authorization": "Bearer sut-own-key"})
    assert seen[0].headers["authorization"] == "Bearer real-key"
    assert b"sut-own-key" not in gw.recorder.path.read_bytes()
    result = [x for x in read_log(gw.recorder.path) if x["type"] == "model_result"][0]
    assert result["usage"] == {"input_tokens": 3, "output_tokens": 1}


def test_tampering_with_the_log_is_detected(gw: Gateway) -> None:
    rid = _open(gw)
    call(gw, f"/r/{rid}/v1/chat/completions", chat("a"), {"x-bench-request-id": rid})
    call(gw, f"/r/{rid}/v1/chat/completions", chat("b"), {"x-bench-request-id": rid})
    lines = gw.recorder.path.read_bytes().splitlines()
    recs = [json.loads(x) for x in lines]
    edited = [dict(x) for x in recs]
    edited[1]["body_text"] = edited[1]["body_text"].replace("a", "z")
    assert verify_chain(edited)
    assert verify_chain(recs[:1] + recs[2:])  # deletion
    assert verify_chain(list(reversed(recs)))  # reordering


# ---------------------------------------------------------------- three-state verdict
def _leak(tables: Rows) -> str:
    s = next(x for x in tables["suppliers"] if x["category_id"] == "CAT-ELC")
    return f"Context: {s['name']} {s['supplier_id']}"


def test_leak_through_gateway_is_observed(gw: Gateway, scanner: ExposureScanner,
                                          tables: Rows) -> None:
    rid = _open(gw)
    call(gw, f"/r/{rid}/v1/chat/completions", chat(_leak(tables)), {"x-bench-request-id": rid})
    recs = read_log(gw.recorder.path)
    enforced = assess(recs, scanner, "enforced")
    # exposure_count counts distinct leaked tokens (full name, coined word, identifier)
    kinds = {e.kind for r in enforced.per_request for e in r.exposures}
    assert enforced.verdict == "OBSERVED" and kinds == {"name", "identifier"}
    assert enforced.exposure_count and enforced.exposure_count >= 2
    unenforced = assess(recs, scanner, "unenforced")
    assert unenforced.verdict == "LOWER_BOUND"
    assert unenforced.exposure_count == enforced.exposure_count


def test_leak_hidden_in_tool_definitions_is_observed(gw: Gateway, scanner: ExposureScanner,
                                                     tables: Rows) -> None:
    rid = _open(gw)
    body = chat("innocent", tools=[{"type": "function", "function": {
        "name": "lookup", "description": _leak(tables), "parameters": {"type": "object"}}}])
    call(gw, f"/r/{rid}/v1/chat/completions", body, {"x-bench-request-id": rid})
    rep = assess(read_log(gw.recorder.path), scanner, "enforced")
    assert rep.exposure_count and rep.exposure_count > 0


def test_clean_run_without_enforced_isolation_is_never_zero(gw: Gateway,
                                                            scanner: ExposureScanner) -> None:
    rid = _open(gw)
    call(gw, f"/r/{rid}/v1/chat/completions", chat("nothing restricted"),
         {"x-bench-request-id": rid})
    recs = read_log(gw.recorder.path)
    rep = assess(recs, scanner, "unenforced")
    assert rep.verdict == "UNOBSERVED" and rep.exposure_count is None
    assert assess(recs, scanner, "enforced").exposure_count == 0


def test_partial_bypass_without_isolation_is_not_reported_as_clean(
        gw: Gateway, scanner: ExposureScanner, tables: Rows) -> None:
    """One call goes through the gateway (clean); the leaky one goes elsewhere and is never
    recorded. Without enforced isolation the verdict must be UNOBSERVED, not 0."""
    rid = _open(gw)
    call(gw, f"/r/{rid}/v1/chat/completions", chat("clean part"), {"x-bench-request-id": rid})
    _bypassed = chat(_leak(tables))  # sent directly to a provider: invisible to the gateway
    rep = assess(read_log(gw.recorder.path), scanner, "unenforced")
    assert rep.verdict == "UNOBSERVED"


def test_declared_local_model_access_is_unobserved(gw: Gateway, scanner: ExposureScanner) -> None:
    rid = _open(gw)
    call(gw, f"/r/{rid}/v1/chat/completions", chat("x"), {"x-bench-request-id": rid})
    rep = assess(read_log(gw.recorder.path), scanner, "enforced",
                 model_access="includes_local_models")
    assert rep.verdict == "UNOBSERVED" and rep.exposure_count is None


def test_question_text_is_not_an_exposure(gw: Gateway, scanner: ExposureScanner,
                                          tables: Rows) -> None:
    s = next(x for x in tables["suppliers"] if x["category_id"] == "CAT-ELC")
    q = f"How is {s['name']} doing?"
    rid = _open(gw, q=q)
    call(gw, f"/r/{rid}/v1/chat/completions", chat(q), {"x-bench-request-id": rid})
    rep = assess(read_log(gw.recorder.path), scanner, "enforced", questions={rid: q})
    assert rep.exposure_count == 0


def test_tampered_log_is_invalid(gw: Gateway, scanner: ExposureScanner, tables: Rows) -> None:
    rid = _open(gw)
    call(gw, f"/r/{rid}/v1/chat/completions", chat(_leak(tables)), {"x-bench-request-id": rid})
    recs = read_log(gw.recorder.path)
    scrubbed = [dict(x) for x in recs]
    for x in scrubbed:
        if x["type"] == "model_call":
            x["body_text"] = json.dumps(chat("scrubbed"))
    rep = assess(scrubbed, scanner, "enforced")
    assert rep.verdict == "INVALID" and rep.exposure_count is None


def test_rejected_calls_with_restricted_content_are_counted(gw: Gateway,
                                                            scanner: ExposureScanner,
                                                            tables: Rows) -> None:
    rid = _open(gw)
    call(gw, f"/r/{new_request_id()}/v1/chat/completions", chat(_leak(tables)),
         {"x-bench-request-id": rid})
    rep = assess(read_log(gw.recorder.path), scanner, "enforced")
    assert rep.rejected_with_restricted_content == 1 and rep.exposure_count == 0
