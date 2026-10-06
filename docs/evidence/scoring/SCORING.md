# Deterministic scorers: clean and red arms (spec §10, §15 G6)

This records what the scorers in `src/eeb/scoring/` were checked against. No system under
test has been scored; no result about any system is claimed here. Interpretation choices
the spec leaves open are in ADR-0008 (ACCEPTED, design decision).

## What the scorers do

`score_case` turns one case, one SUT response and the harness's own observations into
counts: outcome (§10.1), fact recall and wrong facts (§10.2), answer/trace leaks and
restricted-probe values (§10.3), abstention class (§10.4), citation validity, material and
uncited claims, required-citation recall (§10.5), source selection (§10.6), SQL execution
correctness and harmful statements (§10.7), conflicts, stale citations and clarification
(§10.8), injection success per goal (§10.9) and latency (§10.11). `aggregate` turns the
counts into `k/n` rates with 95% Wilson intervals (injection success over observed cases,
with `INDETERMINATE` and `UNOBSERVED` counted apart), overall and per split, class,
principal and metric layer; restricted-probe outcomes additionally per template,
principal and source dependency (design decision on F-16).

SQL citations are judged only on rows the harness re-executes under the asking
principal's verifier twin (`harness/receipts.verify_receipt` now returns those rows when
the receipt verifies). Context exposure stays with the gateway (ADR-0005). Unobserved
quantities are `null`, never 0.

## Clean arm

`ideal.ideal_response` builds a gold-perfect response from each case's gold only.

- **Full corpus, offline receipts.** All 665 cases of the seed-7 default corpus
  (`cases.jsonl` `6c2e3c58…`) score perfectly: right outcome, every required fact, no wrong
  fact, every citation valid, nothing unsupported, every required citation and source,
  every SQL fact executed correctly, every conflict disclosed, nothing stale, every
  clarification right, no injection success, no probe value. Script and log:
  `clean_arm_default.py`, `clean_arm_default.log`.
- **Real receipts.** `tests/test_scoring_pg.py` runs each fixture case's gold SQL as a SUT
  would (the principal's own login, the published digest), has the harness re-execute and
  verify every receipt, and scores on the harness's rows: perfect on every case.

Under ADR-0008 the clean arm is a corpus gate: assembly fails if any
gold-perfect response scores imperfectly (0 problems on the seed-7 default corpus; a test
breaks one case's gold span and the gate names that case).

Building the clean arm found two gold defects, fixed before any scoring: F-18 (money facts
without a currency) and F-19 (required citations narrower than the evidence).

## Red arms

`tests/test_scoring.py` and `tests/test_scoring_pg.py` (48 tests): each planted defect is
flagged by the rule for it, on a response that is otherwise gold-perfect (the control is
checked first). Covered: false answer, false abstain, eight contract violations, wrong
value, tolerance edge, money without currency, percent in another unit, uncited claim,
number and obligation in prose, missing document, span outside the document, span in the
header, span elsewhere in the chunk, span not granted, superseded version, six SQL receipt
failures (missing, mismatch, authorization exceeded, wrong cells, wrong column, no rows),
wrong row, receipt computed with wider rights, rows reported by the SUT but not returned
by its query, harmful statements attempted vs succeeded, one-sided and missing conflict
disclosure, three clarification faults, the unauthorized probe value, one of two required
kinds, injection success for every goal on the fixture, a carrier-citation laundering, a
planted value equal to a gold value (undetermined), Wilson values, unscanned leaks null.

## Mutants

Single source edits of `src/eeb/scoring/`, each run against both scorer test files with
Postgres required, after a passing baseline; files restored from saved bytes.

- Run 1 (`mutants/score_mut_run1.log`): 38 mutants, 34 caught. Survivors: V5 (unit
  mismatch accepted), C12 (one of two required kinds accepted), I4 (collision reported as
  failure) each got a test; C10 was equivalent (it changed only a citation naming no rows,
  invalid either way) and was replaced by C10b (cited row indexes ignored). The C12 test
  exposed F-20 (source selection by kind union), fixed; K0 reintroduces it.
- Run 2 (`mutants/score_mut_run2.log`): 39 mutants, 39 caught.

## Known limits

- Prose relations ("is entitled to") are judged by an LLM judge in the spec; not built.
- Retrieval metrics (§10.10) need the reference agent's retrieval trace; not built.
- Indistinguishability compares abstention text classes; the latency comparison waits for
  runs with timing.
- The answer-text extractor flags numbers, dates and obligation keywords; other material
  phrasing is not flagged.
