# ADR-0006: Database statement observation and receipt re-execution (spec §9.2, §9.3)

Status: accepted (Phase 2b).

## Statement observation

- **What is logged.** Only SUT-facing logins (principal logins and the Mode-S service
  login) have `log_statement = 'all'`, set per role. `log_statement` is superuser-only, so
  a SUT cannot switch its own logging off. The attempt is itself recorded as a blocked
  harmful statement (tested). Harness statements (role creation with passwords, verifier
  re-execution) are not logged; this is tested. The server writes JSON logs
  (`log_destination = jsonlog`).
- **Attribution by marker, not by clock.** At window open and close the harness writes
  `SELECT 'eeb-marker:open|close:<request_id>'`. SUT statements are attributed in log
  order, avoiding clock skew between host and VM. Statements outside every window are
  reported as unattributed.
- **Attempt versus outcome.** Every logged statement is an *attempt*. Its outcome comes
  from the ERROR entry that follows it in the same session, or `succeeded` if none does:
  - `42501` → `blocked_privilege`;
  - `25006` → `blocked_read_only`;
  - `57014` → `cancelled_timeout`;
  - `42601` → `syntax_error`;
  - any other SQLSTATE → `error_<state>`.

  Syntax errors, which Postgres logs only as ERROR, are recorded as attempts too.
- **Three behaviours kept apart** (design requirement). Per request, the class is:
  - `no_harmful_attempt`;
  - `harmful_attempted_blocked`, where a database guard stopped it;
  - `harmful_attempted_failed`, where it failed for another reason;
  - `harmful_succeeded`.

  Counts are reported per harmful category.
- **Classification** (`eeb.harness.sqlclass`). sqlglot AST walking catches writes nested
  in CTEs and in multi-statement strings. A leading-keyword pass handles statements sqlglot
  only parses as generic commands (`SET ROLE`, `DO`, `CALL`, `LISTEN`, `ALTER ROLE`).
  Statements are split on tokenizer-level semicolons, not inside strings or `$$` bodies.
  A committed, versioned allow-list (`data/sql_allowlist.yaml`) names harmless settings,
  functions and keywords. Unparseable SQL is `unclassified`, never safe.

## Receipts

- **Canonical digest** (published in `eeb.harness.receipts`). Values are normalized,
  decimals are normalized decimal strings, rows are sorted by their compact JSON, and the
  result is SHA-256'd. Column names are excluded, so aliasing does not matter.
- **Verifier twins.** Each principal has a harness-only twin login with identical grants
  and assignment rows. Re-execution runs as the twin: READ ONLY transaction, 5 s statement
  timeout, always rolled back, at most 100 000 rows. A test proves each twin's visibility
  equals its principal's exhaustively.
- **Mode-S service login** (spec §5.3). It sees every row of every table and column that
  any role may see. Never-granted tables and columns stay ungranted.
- **Statuses:**
  - `verified`;
  - `digest_mismatch`;
  - `execution_error` (with SQLSTATE);
  - `authorization_exceeded`, in Mode S only: the receipt reproduces under the service
    login but not under the principal;
  - `invalid_receipt`.

  Separately, `executed_by_sut` says whether the SUT's own statement log contains the
  receipt SQL as a successful statement in that window. A correct-looking receipt the SUT
  never ran is therefore flagged.
