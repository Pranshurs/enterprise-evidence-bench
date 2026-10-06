# Phase 2b, part 1: recording gateway and observation boundary

Design: ADR-0005. Local run: macOS arm64, Colima linux/arm64, Docker 29.5.2,
`python:3.13-slim` and `postgres:17` images.

## Gateway (in-process, `tests/test_gateway.py`, 22 tests)

| Property | Evidence |
|---|---|
| Every call is recorded before forwarding, in a hash-chained log; edits, deletions and reordering are detected | `test_compliant_call_is_recorded_and_answered`, `test_tampering_with_the_log_is_detected` |
| Attribution by an unguessable per-request path. A missing or mismatched header, unknown id, closed window, non-JSON body, streaming request or unknown endpoint is **rejected, recorded as an anomaly, and never reaches a model** | `test_rejections_are_recorded_and_never_reach_the_model` (8 cases) |
| Control plane requires the run secret | `test_control_plane_requires_the_secret` |
| Scored runs are strictly sequential; request ids are single-use | `test_strict_sequential_windows`, `test_request_ids_are_single_use` |
| Credential custody: the SUT's own key is replaced by the gateway's key and never logged | `test_sut_credentials_are_replaced_by_gateway_custody` |
| Every string in the body is scanned, including tool definitions | `test_leak_hidden_in_tool_definitions_is_observed` |
| Three-state verdict (never 0 without proof); question text excluded; tampered log is INVALID; rejected calls carrying restricted content are counted | `test_leak_through_gateway_is_observed`, `test_clean_run_without_enforced_isolation_is_never_zero`, `test_partial_bypass_without_isolation_is_not_reported_as_clean`, `test_declared_local_model_access_is_unobserved`, `test_question_text_is_not_an_exposure`, `test_tampered_log_is_invalid`, `test_rejected_calls_with_restricted_content_are_counted` |

## Isolation under Docker (`tests/test_isolation_docker.py`, 4 tests)

| Run | Probe (before and after the request) | SUT bypass attempt | Verdict |
|---|---|---|---|
| internal network, SUT leaks through the gateway | external IP ✗, DNS ✗, host ✗, default route ✗, gateway ✓, db ✓ → `enforced` | n/a | **OBSERVED**, ≥ 3 exposures (name, identifier, canary) |
| internal network, bypass SUT | `enforced` | TCP to 1.1.1.1:443 **blocked** from inside the SUT | **OBSERVED, 0** |
| ordinary bridge (negative control), bypass SUT | external egress detected → `unenforced` | egress **open** | **UNOBSERVED** (not 0) |
| ordinary bridge, SUT leaks through the gateway | `unenforced` | n/a | **LOWER_BOUND** ≥ 3 |

The probe runs in the SUT's network namespace from a separate container, so it does not
depend on anything inside the SUT image. The bypass test never sends content to an external
service; it only attempts a TCP connection.

## Open design question

**In-process models** (ADR-0005). Egress control cannot see a model running inside the SUT
container. The proposal: a `model_access` declaration, with anything other than
`gateway_only` reported UNOBSERVED, and undeclared in-process inference stated as a
limitation. This needs ratification before the scorer freezes.

## Not yet built in Phase 2b

- metric layer and `security_invoker` views;
- case templates, gold builder and frozen case files;
- Postgres statement-log observation;
- receipt re-execution;
- the remaining scorers;
- baselines B1–B3.

## Addendum (ADR-0005 acceptance)

The in-process inference tests added with ADR-0005 demonstrate that an undeclared
in-process inference path can evade the observation boundary. A false `gateway_only`
declaration is outside what the harness can independently falsify. OBSERVED applies only
to `externally_mediated_model_context` and is not proof that no embedded inference
occurred.
