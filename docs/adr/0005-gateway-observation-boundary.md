# ADR-0005: Recording gateway and observation boundary (spec §9.1, §10.3)

Status: accepted for implementation (Phase 2b), except the in-process-model item, which is
**proposed for a design decision**.

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
   - `UNOBSERVED`: isolation is not enforced and the gateway saw 0 exposures, **or** the
     SUT declared any model access that does not go through the gateway. **Never reported
     as 0.**
7. **Enforced isolation (Docker).**
   - The SUT container runs on an `internal: true` network whose only other members are
     the gateway (data port) and the benchmark database.
   - Before scoring, and again after it, a **sidecar probe** runs inside the SUT's network
     namespace (`--network container:<sut>`), independent of the SUT image. It checks
     three things: external egress fails (IP and DNS), the gateway is reachable, and the
     database is reachable.
   - Only a run whose pre- and post-probes both pass is `isolation: enforced`.
   - The gateway's control plane requires a per-run secret held only by the harness.

## Proposed design decision: in-process models

Egress control cannot observe a SUT that runs a model **inside its own container**, for
example an embedded local LLM. Such traffic never crosses the network.

Proposed handling:
- The SUT setup declares `model_access` (`gateway_only` | `includes_local_models`). Any
  declaration other than `gateway_only` makes context exposure `UNOBSERVED`.
- Undeclared in-process inference is stated as a **limitation of the observation
  boundary**, not claimed as covered.

This changes what a review may attack, so it requires a recorded design decision before the scorer freezes.
