# Case corpus: build, validation and closure (status: UNFROZEN)

This records what was run on the case corpus and what it showed. The corpus is **not
frozen**. Two steps need a person and are open:

1. review of the 68-case stratified review set (`review_set.jsonl`; fill `reviewed`,
   `issue`, `resolution`);
2. paraphrase of the 77 queued questions (89 test cases, 22.3% of the 399 test cases) by a
   model family independent of the systems under test, and validation that each
   paraphrase keeps its meaning.

Scorers and baselines have not been run. No result about any system is claimed here.

## What was built

`eeb cases build` on the seed-7 default instance (`58c86fb8…`, 103,832 PO lines):

| | |
|---|---|
| cases | 665 (399 test, 266 dev), one per slot of the frozen plan |
| families | 600: 535 single cases, 65 counterfactual pairs |
| families used by more than one single case | 0 |
| classes | X 285, A 65, and 45 each of S, D, C, T, U, Q, H |
| expected outcome | ANSWER 465, ABSTAIN 155, CLARIFY 45 |
| injection overlays | 50 |
| out-of-layer | X 40, S 30 |
| `cases.jsonl` sha256 | `42e4b8a6eedea271db17755be3e747d721926c4a51020be32b6abe64d5263f8c` |

Per-template counts, principals and every rejected-candidate reason are in
`BUILD_REPORT.json` next to the cases.

## Checks and their results

- **Cross-source necessity.** All 285 X cases: SQL alone insufficient and documents alone
  insufficient (0 and 0 exceptions).
- **Metric-layer membership.** All 70 designated out-of-layer cases are not reconstructible
  by any governed metric query in the search space; all 260 designated in-layer S and X
  cases are.
- **SQL gold under the asker's login.** 402 SQL gold facts re-executed in Postgres through
  the asking principal's own login (10 principals): 0 mismatches, 0 errors. 1 restricted
  probe value re-executed with administrator rights: matches.
- **Determinism.** Two builds under different hash seeds are byte-identical.
- **Cross-interpreter identity.** Python 3.11.17, 3.12.13, 3.13.16 and 3.14.8 emit identical
  bytes for seed 7 small, seed 1234 small, seed 11 default, and seed 7 default *including
  the five corpus files*. Record: `cross_version_digests.json`.
- **Two fresh containers.** Instance (445 files) and corpus (5 files) byte-identical across
  two new `postgres:17` containers; oracle, database and verifier-twin authorization digests
  equal; 0 disagreements; the gold SQL check passed on both; a rebuild of corpus A found no
  difference. Record: `fresh_container_closure.json`.
- **Tests.** 322 passed, 0 failed (Postgres and Docker tests required, not skipped).

## Can the checks fail?

- The optimized metric-layer search is compared with an exhaustive uncached search; 9 of 9
  single mutants of the optimized code are caught.
- Validators, builder rules and the Postgres gold check: 19 of 19 single mutants caught, on
  a passing baseline. The first version of the tests caught 15 of 18; the three survivors
  each got a targeted test (see F-11 in `docs/FINDINGS.md`).
- `eeb cases verify` rejects an edited machine field in each corpus file, a missing file
  and a manifest built by different code, and accepts filled-in review and rephrase fields.

Mutants were run from a script outside the repository and are not part of the test suite.

## Known limits

- One case of 665 carries a restricted-value probe.
- `S.unpaid_invoices` and `X.off_contract_compliance` produce no case under the validators.
- Three templates supply 245 of the 285 X cases, including all 65 permitted pair members
  (`X.raw_otd_gap` 85, `X.rejection_within_threshold` 81, `X.raw_otd_met_target` 79). The
  other two supply 30 and 10.
- The search space of the out-of-layer validator is the one stated in
  `cases/validate.py`: catalog metrics, filters from the case's own slots, grouping on up
  to two declared dimensions. It does not consider combining several metric results.
- The small fixture cannot fill the plan; tests bind a sub-plan.
