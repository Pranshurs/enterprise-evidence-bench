# ADR-0005: Recording gateway and observation boundary (spec §9.1, §10.3)

**Status: ACCEPTED (design decision 2026-10-06), with the scope wording in "In-process models"
below.**

## Problem

Context exposure must be measured where the system under test (SUT) cannot edit the
evidence. That requires answering four questions for each benchmark request:
1. What text reached a model?
2. Under which request was it sent?
3. Did any model traffic escape observation?
4. How confident is each reported number?

## Decisions

1. **The gateway is the only holder of provider credentials.** The SUT receives a gateway
   base URL and a dummy key. The gateway strips any SUT `Authorization` or `api-key`
   header and attaches the real credential itself. A SUT that bypasses the gateway must
   therefore bring its own credentials. That is detectable in setup review and impossible
   under enforced isolation.
2. **Attribution comes from the path, not from trust.**
   - The harness issues every request id as a 128-bit random token.
   - Model endpoints live under `/r/{request_id}/...`, and the path is authoritative.
   - `X-Bench-Request-Id` must equal the path id; a mismatch is recorded as an anomaly and
     rejected.
   - A call is accepted only while that request's *window* is open (opened by the harness
     before `POST /v1/ask`, closed after the response plus a grace period). Unknown,
     closed or never-opened ids are rejected and recorded as anomalies.
   - Scored runs execute requests **sequentially** (one open window), so a SUT cannot
     shift one request's model traffic onto another request's id. Concurrency is allowed
     only in load runs, whose exposure results are labelled `attribution: concurrent`.
3. **Recording.**
   - Every call is appended to a hash-chained JSONL log (`prev_hash`, record hash over
     canonical JSON) before forwarding. Each record holds the exact request body text, the
     response body, usage, status, and whether the call reached a model.
   - Exposure scanning reads **every string value** in the JSON request body: messages,
     system prompts, tool definitions and tool results. The SUT cannot hide context in an
     unscanned field.
   - Non-JSON bodies are rejected and logged.
4. **Upstreams.** Each upstream is one of:
   - a deterministic scripted model (CI and offline tests);
   - OpenAI-compatible forwarding (including Azure OpenAI deployment URLs);
   - Anthropic Messages forwarding.

   Streaming is **not supported in v1**. Streaming requests are recorded and rejected with
   an explicit error, not silently degraded.
5. **Tokens and cost** come from gateway-recorded provider usage. Cost uses a committed,
   dated price table. Models absent from the table are reported `unpriced`; no price is
   invented.
6. **Isolation and the three-state verdict.** Context exposure for a run is one of:
   - `OBSERVED`: isolation is **enforced and proven by an active probe** (below). Counts
     are definitive for the attack families run.
   - `LOWER_BOUND`: isolation is not enforced, but the gateway still saw ≥ 1 exposure.
     The count is reported as "at least n".
   - `UNOBSERVED`: isolation is not enforced and the gateway saw 0 exposures, **or**
     `model_access_mode` is not `gateway_only`, **or** there is evidence of an unobserved
     inference path. **Never reported as 0.** Observed events are still reported.
7. **Enforced isolation (Docker).**
   - The SUT container runs on an `internal: true` network whose only other members are
     the gateway (data port) and the benchmark database.
   - Before scoring, and again after it, a **sidecar probe** runs inside the SUT's network
     namespace (`--network container:<sut>`), independent of the SUT image. It checks
     three things: external egress fails (IP and DNS), the gateway is reachable, and the
     database is reachable.
   - Only a run whose pre- and post-probes both pass is `isolation: enforced`.
   - The gateway's control plane requires a per-run secret held only by the harness.

## In-process models (design decision)

> Model-context exposure is only eligible for an OBSERVED verdict when the run uses the
> harness-enforced gateway-only execution profile and the SUT declares that all model
> inference is gateway-mediated. The declaration defines applicability; it does not
> independently establish coverage.
>
> If the SUT declares embedded/in-process inference, does not declare its model-access
> mode, or the harness detects evidence of an unobserved inference path, the headline
> context-exposure verdict is UNOBSERVED. Any gateway-observed exposures may still be
> reported as observed events, but they must not be presented as a complete exposure count
> or as zero leakage.
>
> Network isolation proves only the absence of ordinary network egress paths covered by
> the harness. It does not prove the absence of inference occurring wholly inside the SUT
> process/container.

Every result carries two protocol fields:
- `model_access_mode` ∈ `gateway_only | embedded | mixed | undeclared`. The default is
  `undeclared`, and missing or unknown declarations map to it.
- `observation_scope` = `externally_mediated_model_context`.

`OBSERVED` therefore means "complete within the externally mediated model context under
enforced isolation". It does not mean every token any model consumed.

**Demonstrated, not only documented** (`tests/test_isolation_docker.py`):
- *Honest declarations.* A SUT feeds restricted context to an in-process "model" and sends
  only the question through the gateway. Declared `mixed`, `embedded` or undeclared, it is
  UNOBSERVED in every case.
- *False declaration.* The same SUT falsely declaring `gateway_only` gets OBSERVED/0 within
  scope, and the locally consumed restricted tokens are absent from everything the
  harness recorded.

**What this test does and does not show** (scope clarification, 2026-10-06):

> The adversarial test demonstrates that an undeclared in-process inference path can evade
> the observation boundary. A false `gateway_only` declaration is therefore outside what
> the harness can independently falsify. OBSERVED applies only to
> `externally_mediated_model_context` and must not be interpreted as proof that no
> embedded inference occurred.

The test is valuable because it fixes the boundary of the claim. It does not detect false
declarations, and nothing in this benchmark claims that it does.
