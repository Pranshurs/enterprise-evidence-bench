# v0.1.0-alpha — Research Preview

A benchmark harness for permission-aware enterprise question answering over SQL and
documents together. This is a **research preview**: the benchmark's machinery is built and
checked; the experiments it exists to run have not been run.

## Status

| Area | Status |
|---|---|
| Automated corpus, gold and scorer evidence | PASS |
| Generated 665-case corpus | UNFROZEN |
| Manual quality review: 70 benchmark cases | PENDING (0/70) |
| Independent-family paraphrasing (77 queued questions) | PENDING — external model access unavailable |
| Baselines B1–B3: implementation and offline plumbing | PASS |
| Live B1 / B2 / B3 performance | NOT RUN |
| Reference agent (spec §12) | NOT IMPLEMENTED; live evaluation NOT RUN |
| Governed metrics vs model-written SQL experiment (spec §13) | NOT RUN |

**No live model-performance claims are made.** Offline runs use a scripted model and prove
only that the harness, baselines, gateway, database and scorers are connected correctly.

## In this release

- Deterministic synthetic procurement enterprise (India/EU/UK, INR/EUR/GBP, ~104k PO lines,
  versioned governed documents); byte-identical across Python 3.11–3.14.
- One access policy implemented twice: PostgreSQL grants and row-level security, and an
  independent Python oracle; generation fails on any disagreement.
- Governed metric layer (`security_invoker` views, caller-equivalence checked).
- 665-case corpus builder with mechanically validated gold: exact document spans, SQL gold
  re-executed under the asking principal's login (427 facts, 0 mismatches), cross-source
  necessity, out-of-metric-layer proofs, counterfactual principal pairs, restricted-value
  probes, injection carriers owned by one split, content and gold-contract gates.
- Recording model gateway (hash-chained log; `UNOBSERVED` is never scored as 0), Docker
  isolation runner, PostgreSQL statement-log attribution, receipt re-execution under the
  asker's authority (`authorization_exceeded` when only a broader login reproduces it).
- Deterministic scorers for spec §10 with clean and red arms; a gold-perfect response scores
  perfectly on all 665 cases.
- Framework-free baselines B1 (document RAG), B2 (+ constrained text-to-SQL on a service
  login), B3 (+ prompt-only access control), and the `eeb run` harness loop.
- Freeze tooling: `eeb cases freeze` refuses until the manual quality review and independent
  paraphrases are complete and bound by provenance; `verify-frozen` detects any change.
- Mutation testing: corpus gates 26/26, validators/builder/gold check 45/45, carrier rule
  10/10, gold contract 7/7, scorers 39/39, freeze provenance 5/5.

Evidence records: `docs/evidence/`. Findings fixed during construction: `docs/FINDINGS.md`.

## Next

Manual quality review and independent paraphrasing → corpus freeze → pinned OpenAI model → B1–B3 ×3
under enforced isolation → reference agent → the §13 experiment.
