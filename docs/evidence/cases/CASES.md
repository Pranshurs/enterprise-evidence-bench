# Case corpus: build, validation and closure (status: UNFROZEN)

This records what was run on the case corpus and what it showed. The corpus is **not
frozen**. Two steps need a person and are open:

1. review of the 70-case stratified review set (`review_set.jsonl`, 70 of the 399 test
   cases, 17.5%; fill `reviewed`, `issue`, `resolution` only);
2. paraphrase of the 77 queued questions (89 test cases, 22.3% of the 399 test cases) by a
   model family independent of the reference agent's, then `eeb cases verify
   --reference-family <family>` to check each paraphrase mechanically.

`eeb cases verify` reports both as freeze blockers until they are done. Scorers and
baselines have not been run. No result about any system is claimed here.

Records are kept unchanged per build: `cross_version_digests.json` and
`fresh_container_closure.json` (generator label 2a.3, commit `656edb7`); `_2a4` (commit
`e66ae00`, before F-17); `_f17` (commit `060daaa`, injection carriers split-owned); `_f19`
(commit `414971c`: money facts carry their currency, F-18; required citations equal the
evidence, F-19); `_adr8` (this build: the scorers' clean arm is a corpus gate, ADR-0008;
`cases.jsonl` unchanged, manifest records `clean_arm_problems`).

## What was built

`eeb cases build` on the seed-7 default instance (generator 2a.4, `51e44c72…`, 103,832 PO
lines; authorization digest `1eb3b4c1…` unchanged). Full build with the gold SQL check:
about 70 s of case building, 2 min 47 s wall time including two database builds.

| | |
|---|---|
| cases | 665 (399 test, 266 dev), one per slot of the frozen plan |
| families | 600: 535 single cases, 65 counterfactual pairs |
| families used by more than one single case | 0 |
| classes | X 285, A 65, and 45 each of S, D, C, T, U, Q, H |
| X share | test 171/399, dev 114/266 (42.9% each) |
| expected outcome | ANSWER 465, ABSTAIN 155, CLARIFY 45 |
| injection overlays | 50 (G1 9, G2 7, G3 14, G4 12, G5 8) over 32 carrier documents: 11 read only in dev, 21 only in test, 0 in both (F-17) |
| out-of-layer | X 40, S 30 |
| active templates | 33, every one binding at least one case; `S.unpaid_invoices` retired |
| X templates | 12; largest 41/285 (14.7%), three largest 125/285 (43.9%), smallest 6 |
| restricted-value probes | 73 cases; 43 of 399 test cases (10.8%) |
| `cases.jsonl` sha256 | `6c2e3c58ea718325e098139b5951c41c503cbad44f3be89e0f292ea1e6751e2a` |
| metric catalog | version 2, sha256 `2d031cde…` |

X cases per template: `X.raw_otd_met_target` 42, `X.rejection_within_threshold` 42,
`X.average_off_contract_order` 41, `X.order_value_against_threshold` 41, `X.raw_otd_gap`
41, `X.rebate_accrual` 32, and 8 each for `X.exception_required_value`,
`X.expedited_lines_and_surcharge`, `X.off_contract_compliance`,
`X.paid_late_under_signed_terms`, `X.service_credit_entitlement`; `X.indexation_observed` 6.

Probe cases span buyer_in, buyer_eu, ap_eu, ap_in and ap_uk (all three business units), S
and X classes, SQL-only and cross-source questions; exact counts per template, principal
and source dependency are in `BUILD_REPORT.json` (`restricted_probe`).

Per-template counts, principals and every rejected-candidate reason are in
`BUILD_REPORT.json` next to the cases. The manifest binds the generator version, config
digest, policy digest, metric catalog digest, plan digest, the digest of every source file
that decides the corpus, and the content-gate summary.

## Checks and their results

- **Freeze gates.** Assembly fails unless the spec §7 minimums, the content requirements
  (X template caps, probe share, X template minimum, no unused or retired template),
  plan conformance and one-question-per-family hold. Problems found: 0.
- **Cross-source necessity.** All 285 X cases: SQL alone insufficient and documents alone
  insufficient (0 and 0 exceptions).
- **Metric-layer membership.** All 70 designated out-of-layer cases are not reconstructible
  by any governed metric query in the search space; all 260 designated in-layer S and X
  cases are.
- **Gold contract.** Every money fact names its currency; every answer fact has a required
  citation whose kinds equal the sources of its whole evidence closure (re-derived at
  assembly independently of the builder). 0 problems; 221 on the corpus before F-18/F-19.
- **Injection carriers.** Every carrier is assigned to one split before binding (seed and
  carrier id, per attack goal, in proportion to each split's injection slots). 0 of 32
  carriers are read in both splits; each split covers G1–G5. Before F-17: 5 of 33 crossed.
- **SQL gold under the asker's login.** 427 SQL gold facts re-executed in Postgres through
  the asking principal's own login (12 principals): 0 mismatches, 0 errors. 104 restricted
  probe values re-executed with administrator rights: all match.
- **Determinism.** The development build and the evidence build give the same
  `cases.jsonl` bytes (`6c2e3c58…`).
- **Cross-interpreter identity.** Python 3.11.17, 3.12.13, 3.13.16 and 3.14.8 emit identical
  bytes for seed 7 small (`d66e4d65…`, 146 files), seed 1234 small (`c54cb2d2…`), seed 11
  default (`2f55d34b…`, 442 files) and seed 7 default *including the five corpus files*
  (`51e44c72…`, 449 files). Record: `cross_version_digests_adr8.json`.
- **Two fresh containers.** Instance (445 files) and corpus (5 files) byte-identical across
  two new `postgres:17` containers; on each, oracle, database, verifier-twin and recorded
  authorization digests equal, 0 disagreements, 0 twin disagreements, 0 service-visibility
  and 0 hardening problems, database reload identical, 29 instance gold SQL facts with 0
  mismatches; instance A verified on container B; the case gold SQL check (427 facts, 103
  probes) passed on both; a rebuild of corpus A found no difference. Record:
  `fresh_container_closure_adr8.json`.
- **Clean arm.** A gold-perfect response scores perfectly on all 665 cases; assembly fails
  otherwise (ADR-0008). Problems: 0.
- **Tests.** 470 passed, 0 failed, 0 skipped on each of Python 3.11.17, 3.12.13, 3.13.16
  and 3.14.8 (Postgres and Docker tests required). ruff clean; mypy strict on `src` clean.
- **Original frozen spec.** `docs/spec.md` sha256 `85204993…cee1c0`, unchanged.

## Can the checks fail?

All mutants below were single source edits run against a passing baseline; each file was
restored from saved bytes afterwards and the suite re-run green. Definitions and logs are in
`mutants_2a4/`.

- **Injection-carrier rule (F-17).** 10 of 10 mutants caught (gate, allocation, builder);
  the first run caught 9, the survivor (one cursor for both splits) got a test
  (`carrier_mut.log`, `B3_rerun.txt`).
- **Gold-contract rules (F-18, F-19).** 7 of 7 mutants caught, including one that
  reintroduces the direct-inputs rule (`contract_mut.log`).
- **Freeze gates.** 26 of 26 mutants caught. The first run caught 24; the two survivors
  (a third member in a counterfactual group; the X share cap moved off its boundary) each
  got a test, including exact-boundary tests for both X caps and the probe share.
- **Validators, builder, metric-layer search and the Postgres gold check.** 45 single
  mutants: the 19 validator, builder and gold-check mutants of the previous campaign; 7
  search mutants unchanged, 3 retargeted to the sorted-value lookup and 2 for its new
  cache; and 14 for this round's code (probe slots and their cursor, least-used ordering,
  `depends_on`, the per-principal row index, the `off_contract` filter). First run
  (`vmut.log`): 44 caught. The survivor
  dropped the `off_contract` slot filter; the exhaustive reference took its filter mapping
  from the code under test. A test that states the mapping itself now catches it (re-run of that mutant alone, recorded
  in `N13_rerun.txt`): 45 of 45.
- **Paraphrase checks.** 16 red arms, one per protected property (identifier, period,
  date, currency, year, supplier name, exclusions, comparison direction, late/on time,
  today/then, off-contract, document anchor, aggregate, wider access ×2, plus provenance
  rules), each against a faithful paraphrase that passes.
- `eeb cases verify` rejects an edited machine field in each corpus file, a missing file
  and a manifest built by different code, and accepts filled-in review and rephrase fields.
  Its freeze report blocks on each open validation step (8 red arms).

## Known limits

- The out-of-layer search space is the one stated in `cases/validate.py`: catalog metrics,
  filters from the case's own slots, grouping on up to two declared dimensions. It does not
  consider combining several metric results.
- The paraphrase check is mechanical: anchors, comparison direction, time anchor,
  qualifiers, document anchors and access wording. It does not prove equivalence of
  meaning; the canonical wording is kept next to each paraphrase for audit.
- 18 of the 50 injection cases read a carrier document another injection case of the same split also reads
  (F-15, accepted). Probe cases concentrate in `X.average_off_contract_order` (F-16,
  accepted with stratified reporting).
- `C.exception_threshold_now` has 3 cases and `S.buyer_caused_late` 5 (F-16).
- The small fixture cannot fill the plan; tests bind a sub-plan.
