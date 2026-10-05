# ADR-0003 acceptance evidence (direct-value exposure coverage)

**Design decision:** ADR-0003 is ACCEPTED subject to direct-value exposure coverage. The
required red arms 1–7 must be detected by the exposure scorer.

| Requirement | Result | Evidence |
|---|---|---|
| Red arms 1–7 detected (name, numeric, identifier, restricted-column value without cell canary, row canary alone, chunk text without canary, wrong-principal value) | Pass | `tests/test_exposure.py::test_red_arm_*` (red arm 6 over three classification/principal pairs) |
| No false positives on authorized content | Pass: 0 exposures over the complete authorized view of 10 principals | `test_everything_a_principal_may_see_scans_clean` |
| Cell-level and supplied-text semantics | Pass | `test_cell_level_semantics_*`, `test_question_supplied_values_are_not_exposures` |
| Scanner falsifiable | Pass: 5 of 5 scanner mutants caught | `test_scanner_mutants_are_caught` |
| Gated material values all registered | Pass: budgets, claims, exceptions, risk scores, rebate rates, primary identifiers, supplier and person names | `test_gated_material_values_are_all_registered`, `test_sensitive_document_values_are_registered` |
| Residual measured | Ungated invoice, payment and line amounts registered at ≥ 80% (asserted); the residual is stated in ADR-0003 | `test_measured_residual_for_ungated_amounts` |

**Generator 2a.2 re-validation** (the generator changed under finding F-4):
- **Cross-version byte identity.** Python 3.11.17 / 3.12.13 / 3.13.16 / 3.14.8 give
  identical artifact digests for seed 7 small (`8a353d9d…`, 126 files), seed 1234 small
  (`730cad99…`, 121 files) and seed 7 default (`5e3133f8…`, 280 files). Record:
  `cross_version_digests.json`.
- **Test matrix.** Each interpreter passes 188 tests with Postgres required and the slow
  test included.
- **Two-fresh-container closure.** The two instances are byte-identical, with 0
  disagreements, 0 hardening problems, 29 of 29 gold SQL facts and authorization digest
  `b539e3dc…` (database = oracle = recorded). `eeb verify` of instance A on container B
  passed. Record: `fresh_container_closure.json`.

The Phase 2a records in `docs/evidence/phase2a/` describe commit `5293876` (generator 2a.1)
and are kept unchanged.
