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
