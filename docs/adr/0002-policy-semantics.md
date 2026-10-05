# ADR-0002: Policy semantics and implementation independence

Status: accepted (Phase 2a).

- **One definition, two independent evaluators.**
  - `src/eeb/data/policy.yaml` is the only policy source.
  - `eeb.policy.schema` only validates its shape and returns the raw mapping.
  - `eeb.policy.sqlgen` compiles it to grants and RLS, and Postgres evaluates them.
  - `eeb.policy.oracle` evaluates it in Python.

  The two evaluators share no code. The database side of the agreement check is measured by
  connecting as each principal's login.
- **A third statement.** `tests/expectations.py` restates key rules from the prose policy
  table without reading `policy.yaml`. Both evaluators are tested against it, so a shared
  misreading of the YAML still fails.
- **Per-assignment evaluation.** A row is visible if *some* active assignment satisfies the
  rule with *that* assignment's parameter. SQL compiles every policy to
  `EXISTS (active assignment a WHERE <rule with a.param_value>)`, matching the oracle.
- **Exactly one role per principal; at most one parameter per role.** A row-level grant
  from one role combined with a column-level grant from another would let Postgres expose
  cells that neither role grants alone. The validator rejects multi-role principals. A
  multi-role design would need per-role views, which is out of scope for v1.
- **NULL semantics.** A NULL compared column never matches. A NULL foreign key never makes
  a parent visible. Default is deny: a table no role lists (`supplier_bank_accounts`) has
  RLS enabled and no grants.
