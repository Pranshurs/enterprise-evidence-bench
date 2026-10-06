# Phase 2b substrate closure (current HEAD)

**Why.** After the Phase 2a closure (`5293876`) and the ADR-0003 closure, the substrate
gained verifier-twin logins, the Mode-S service login, per-role statement logging and a
JSON-log requirement. Those earlier records are left unchanged; this record covers the
substrate as it is now.

**The build gate now also checks** (every `eeb build`):
- verifier twins see exactly what their principals see (exhaustive comparison;
  `verifier_twins` digest);
- the service login sees every row of exactly the union of role grants, and nothing
  never-granted (`service_outcome_digest`);
- logging configuration:
  - `logging_collector` on and `log_destination` including `jsonlog`;
  - server-wide `log_statement` is `none`, so harness statements are unlogged;
  - every SUT-facing login has `log_statement=all`;
  - no verifier twin is statement-logged.

Each new check has a red arm in `tests/test_pg_mutants.py::test_gate_extensions_detect`:
an unlogged SUT login, a narrowed service policy and a twin missing a role grant are all
caught.

**Procedure** (`scripts/fresh_container_closure.py`):
1. `eeb build --seed 7 --scale default` on a fresh `postgres:17` container with JSON
   statement logging.
2. Destroy the container.
3. Run the same build on a second fresh container.
4. `eeb verify` instance A on container B.

**Result** (`substrate_closure.json`):

| Check | A | A verified on B |
|---|---|---|
| passed | ✓ | ✓ |
| authorization digest (database = oracle = recorded = verifier twins) | `b539e3dc…` | `b539e3dc…` |
| service-login visibility digest | `901db369…` | `901db369…` |
| disagreements / twin disagreements / service problems / hardening and logging problems | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| gold SQL facts | 29 of 29 | 29 of 29 |
| reload byte-identical | ✓ | ✓ |

The A and B instances are byte-identical (281 files, instance digest `5e3133f8…`,
unchanged from the ADR-0003 closure because the generator did not change).

The record contains hashes and counts only. Raw Postgres logs are never published
(ADR-0006).
