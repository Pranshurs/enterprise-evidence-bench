# Implementation findings

Findings discovered while building, recorded as they happened. Each has a regression test
or an explicit test of the observed behaviour.

## Phase 2a

**F-1: stale purchase-order index when several S2 suppliers are planted.**
- *Defect.* `planted_s2` built its PO and receipt indexes once, before the loop over S2
  suppliers. When it measured the second supplier, it looked up lines of POs the first
  iteration had just planted, and the lookup raised `KeyError`.
- *Why the small fixture missed it.* It plants one S2 supplier, so the stale index was
  never read after a mutation. The first default-scale run, with three S2 suppliers, failed
  immediately.
- *Fix.* The indexes are rebuilt for each supplier.
- *Regression test.* `tests/test_regressions.py::test_f1_...` forces three S2 suppliers at
  small-scale cost. It was shown to fail on the pre-fix code and pass on the fix.
- *Lesson.* Scale-sensitive paths need a test that exercises them at small cost, not only
  a slow default-scale run.

**F-2: switching to one's own group role fails at the schema privilege, before RLS.**
- *Observation.* After `SET ROLE <ns>_g_category_manager`, reads fail with
  `permission denied for schema eeb`, because only the principal group has schema usage.
  This is a safe *privilege* denial. It says nothing about the RLS predicate.
- *Consequence for the tests.* Privilege-denial tests and RLS tests are kept separate.
  `test_rls_is_reached_and_filters` uses principals that *do* hold relation privilege (the
  query succeeds) and asserts a non-empty, strict, policy-exact subset. Those tests cannot
  pass through outer privilege denial.

**F-3: the login binding is enforced twice; a single deletion is masked, a paired one is
caught.**
- *Observation.* The two layers are:
  - the RLS policy clause `a.login = current_user`;
  - the RLS on `eeb_sec.assignments`, where each login sees only its own rows.

  Removing either layer alone produced no disagreement, because the other layer stayed
  effective. Removing both was detected as cross-principal widening.
- *Classification.* These single deletions are **masked by redundant enforcement**, not
  logically equivalent. `test_login_binding_has_two_layers` asserts all three variants.

## ADR-0003 acceptance

**F-4: some restricted values could not serve as direct-value probes.**
- *Audit.* The audit for the direct-value exposure registry (seed 7 default, generator
  2a.1) found:
  - budget amounts: 0 of 120 distinctive (round planning figures);
  - policy-exception amounts: 1 of 18 distinctive (round S3 totals);
  - one seed had a non-distinctive claim amount;
  - 5 of 737 coined name words collided with dictionary words (`tonal`, `zuni`, `dani`,
    `tarin`, `huspil`; all 4–6 letters).
- *Fix (generator 2a.2, required by the ADR-0003 ruling).* Budget, claim, exception and S3
  PO amounts are drawn with ≥ 6 significant digits, and S3 POs are one lot so the total
  keeps them. A single coined word is a name probe only if it has ≥ 7 letters. Full names
  are always probes.
- *Result.* 0 non-distinctive gated amounts across 4 seeds × 2 scales. The golden digest
  and cross-version evidence were regenerated in `docs/evidence/adr0003/`.

## Case and gold milestone

**F-5: the raw on-time metric divided by a count that can be zero.**
- *Defect.* `raw_on_time_delivery_pct` divided by `count(*)`. A filtered query with no
  grouping that matches no row divides by zero instead of returning NULL.
- *Fix.* The divisor is `nullif(count(*), 0)`, as `rejection_rate_pct` and
  `effective_unit_cost` already had. The Python reference returns `None` for an empty set.
- *Consequence.* The governed catalog is checksum-frozen, so this is a deliberate catalog
  change: `e4a993d6…` became `1a8badb6…`. No baseline had run on the old catalog.

**F-6: a new contract attribute shifted every later random draw.**
- *Defect.* The SLA quality threshold was drawn from the shared `contracts` stream, between
  two existing draws. Every contract's item list changed, and with it the order volume:
  the seed-11 default fixture fell from 103,441 to 97,976 purchase-order lines, below the
  100,000 floor. Seed 7 rose to 109,051, so the seed-7 checks did not show it.
- *Found by.* `test_default_scale_is_deterministic_and_large_enough`, which uses seed 11.
- *Fix.* The threshold has its own stream (`contracts/quality_threshold`). Line counts are
  back to 103,832 (seed 7) and 103,441 (seed 11), the values before the attribute existed.
- *Lesson.* A new attribute gets a new named stream. Inserting a draw into an existing
  stream changes the fixture far from the change.

**F-7: the out-of-layer search rescanned the whole view for every case.**
- *Observation.* `MetricSearch.reconstructible` filtered and regrouped about 100,000 rows
  for every metric, filter subset and grouping, for every candidate case: about 10 s per
  case at default scale (28 cases in 150 s under the profiler), with 94% of the time there.
- *Fix.* Rows are indexed per filter dimension and the numeric results of one (principal,
  metric, filter subset, grouping) query are computed once. The set of queries tried is
  unchanged. A full default-scale build takes about 100 s.
- *Why this is the same validator.* `tests/test_case_validators.py` keeps the plain search,
  which scans the view for every query, and compares the two query by query (the multiset
  of values) and verdict by verdict for every principal. Nine single mutants of the
  optimized search (dropped cache-key parts, ignored filters, lost empty-set case, strict
  tolerance, capped grouping) are each caught.

**F-8: out-of-layer questions answered by a small count rarely survive the validator.**
- *Observation.* With the build fast enough to finish, 13 of the 30 out-of-layer S slots
  could not be filled. Both out-of-layer S templates answered with a count (1, 2, 3 …), and
  some governed metric query, typically `po_count` under some grouping, returns the same
  small integer. `S.unpaid_invoices` was rejected for all 600 bindings.
- *Decision.* The validator is unchanged: it cannot tell coincidence from reconstruction,
  and a case it cannot clear is not an out-of-layer case. Three templates with distinctive
  answers were added instead (`S.adjusted_otd`, `S.late_line_value`, `S.unpaid_amount`).
- *Open.* `S.unpaid_invoices` contributes no case and `X.off_contract_compliance` none
  either (all 30 bindings fail cross-source necessity, because every non-compliant order
  id is also named in an exception memo). Both are kept, unused, pending a design decision.

**F-9: quotas were being met by asking one question as several principals.**
- *Observation.* The 45 conflict cases covered 22 distinct conflicts; the corpus had 665
  cases in 552 families.
- *Fix.* A family (template plus slots) is used once. The two members of a counterfactual
  group are the only cases that share one. The corpus now has 600 families for 665 cases
  (535 single cases and 65 pairs).
- *Consequence for the generator.* The default fixture did not hold 45 distinct conflicts
  (3 thresholds, about 22 usable payment-term conflicts, 3 force-majeure conflicts). The
  default scale now records more ordinary incidents (`incident_draws_per_month = 12`),
  which gives 28 force-majeure conflicts at seed 7 and 36 at seed 11. The small scale keeps
  one draw per month and its bytes did not change.

**F-10: the documents-only check did not search the documents a case cites.**
- *Defect.* To decide whether documents alone answer a question, the validator searched
  documents that name the supplier or contract in the question's slots. An SLA names its
  contract, not the supplier, so for a supplier-and-quarter question the cited SLA itself
  was not searched. A database value printed there would have passed as database-only.
- *Found by.* Writing the red arm for the rule: the constructed violation was accepted.
- *Fix.* The reader's documents are those naming the supplier, the contract or any of the
  supplier's contracts, plus every version of each document the gold cites. The search
  only grew. All 285 X cases still prove both sources necessary; the stricter check rejects
  644 expedite candidates instead of 446.

**F-11: three rules had no test that could fail.**
- *Observation.* Of 18 single mutants of the validators, the builder and the Postgres gold
  check, 15 were caught by the first version of the red-arm tests. The survivors: the
  cited-document search (masked by the contract search for supplier questions), the rule
  that a permitted group member sees the complete answer, and family reuse by groups.
- *Fix.* One targeted test per survivor, and one more mutant for the contract search. All
  19 single mutants are now caught, on a passing baseline.

**Open observation: restricted-value probes are rare.** One case of 665 (seed 7 default)
records an answer that differs from the all-rows answer for its principal. The mechanism is
tested, but the corpus barely exercises it. Whether to bind more partially-visible
principals on purpose is a design decision.
