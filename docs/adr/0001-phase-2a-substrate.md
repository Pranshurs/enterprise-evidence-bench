# ADR-0001: Deterministic substrate for Phase 2a

Status: accepted (Phase 2a).

- **Randomness.** Every draw comes from SHA-256 in counter mode keyed by `(seed, stream
  name)` (`eeb.rng`). Only integers leave the RNG, and all money and rates are `Decimal`;
  floats are rejected by the canonical encoder. Named streams keep scenario planting from
  shifting background data.
- **Canonical artifacts.** The encoding is UTF-8 JSONL with sorted keys and no insignificant
  whitespace. Decimals and dates are written as strings. Rows are sorted by primary key
  (codepoint order; the database uses `C` collation). `INSTANCE.json` records the SHA-256 of
  every file and an instance digest over that map. No wall-clock value, path or credential
  is written.
- **Instance "today".** It is a configuration value (`2026-07-15`). Authorization validity
  is evaluated against it, never against the wall clock. In the database it lives in an
  admin-owned table that principals cannot modify. Session settings cannot move it (tested).
- **Database roles.** Postgres roles are cluster-global, so every role is prefixed with the
  database name (the namespace). Fixture passwords are derived and local-only. They are not
  secrets and never appear in artifacts (tested).
- **Validity on two layers.**
  - Group membership is granted only for roles with an assignment active today.
  - Every RLS policy re-checks the assignment window.

  The `cm_moved` principal (expired MRO assignment plus active ITH assignment) makes the
  second layer observable, and the `active_ignores_valid_to` mutant is detected through it.
- **Base tables now, curated views in 2b.** Spec §5.2 speaks of grants on curated views.
  Phase 2a grants on base tables, with column grants for restricted columns. The
  `security_invoker` views over these tables arrive with the metric layer in Phase 2b, and
  the agreement check will be extended to cover them.
- **Gold derivation twice.** SQL-sourced gold facts are derived in Python from the final
  rows and re-derived by `gold_sql` on the built database inside the `eeb build` gate. Doc
  facts are resolved to exact character spans of a unique phrase.
- **Generation gate.** `eeb build` writes the instance to `<out>.partial`, builds a fresh
  database and runs four checks:
  - reload byte-identity;
  - exhaustive agreement;
  - hardening;
  - gold SQL.

  It renames the directory to `<out>` only if all pass. A disagreement fails generation.
