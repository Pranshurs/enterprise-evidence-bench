# Phase 2b, part 2: database observation and receipt verification

Design: ADR-0006. Postgres 17 fixture with JSON logging; SUT behaviour simulated through
the real principal logins. Tests: `tests/test_db_observation.py` (19 tests) plus twin and
service tests in `tests/test_pg_enforcement.py`.

| Behaviour | Observed class / status |
|---|---|
| SELECT within scope | `no_harmful_attempt` |
| DELETE, never-granted table read, SET ROLE to another group, attempt to disable statement logging | `harmful_attempted_blocked`: 4 attempted, 4 blocked (`blocked_privilege`), 0 succeeded; categories write / never_granted_object / role_or_config |
| Catalog read (`pg_roles`) and a `search_path` change | `harmful_succeeded`: catalog 1, role_or_config 2 |
| `pg_sleep` stopped by statement_timeout | `cancelled_timeout` → blocked; category dangerous_function |
| Syntax error | an attempt, `unclassified` |
| Statement outside any window | unattributed |
| Harness verifier statements and role passwords | absent from the log |
| Receipt run by the SUT | `verified`, `executed_by_sut` |
| Tampered digest | `digest_mismatch` |
| Correct receipt the SUT never ran in its window | `verified` but `executed_by_sut = false` |
| Mode-S receipt broader than the principal | `authorization_exceeded` (Mode P: `digest_mismatch`) |
| Receipt containing a DELETE | `execution_error` (42501/25006); row count unchanged |

**Falsifiability.** Three observer mutants each change the blocked scenario's verdict:
outcome detection that always returns succeeded, a blind classifier, and ignored markers.
Twin agreement is exhaustive over all principals and tables.

**Full suite:** 243 tests with Postgres and Docker required. `ruff` and `mypy --strict`
are clean.
