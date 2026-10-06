# Baselines B1–B3 and the run loop: offline plumbing (no results)

**No baseline has been scored with a real model.** Everything here was run with the
scripted offline model, which proves that the harness, the baselines, the gateway, the
database and the scorers are connected correctly. Scripted replies are chosen by the tests;
the numbers they produce are not baseline performance and are never reported as such
(ADR-0009). Live runs need an OpenAI key supplied through the environment for a specific run.

## What exists

- `src/eeb/harness/run.py`: the run loop. One run = one SUT, one credential mode, a fresh
  database namespace and a fresh recording gateway. SUT setup is bracketed by its own
  database marker window; each case is asked inside a gateway window and database
  markers, strictly one at a time. Afterwards the statement log is attributed, every
  receipt is re-executed under the asker's verifier twin, context exposure is assessed from
  the gateway log, every response is scored (`src/eeb/scoring`) and aggregated. The
  gateway runs in-process on localhost, so isolation is **unenforced**: context exposure is
  `LOWER_BOUND` when something is seen and `UNOBSERVED` otherwise, never a complete 0.
- `src/eeb/baselines/`: B1–B3 as framework-free SUTs (ADR-0009). A paragraph chunker and
  BM25 over every rendered document; one OpenAI-compatible model client through the
  gateway; B2/B3 add one constrained text-to-SQL round (one parsed `SELECT`, row cap 200,
  read-only transaction, 5 s timeout) on the Mode-S service login; B3 adds the principal and
  the instance policy to the system prompt. Controls are constants in `common.py`
  (temperature 0, 1024 output tokens, top-5 passages, up to 3 queries, no retries).
- `eeb run b1|b2|b3`: drives a baseline through the harness on a case selection. The
  scripted upstream marks the report `plumbing_only`; the OpenAI upstream reads
  `OPENAI_API_KEY` from the environment only. A test-split run needs `--purpose` and is
  appended to `TEST_RUNS.log` (spec §11).

## What the plumbing tests show (`tests/test_baselines_offline.py`)

- Every baseline answers every case through the harness; every model call is attributed
  to its request (B1 one call per request, B2/B3 two); no statement is unattributed; the
  baselines' schema introspection is attributed to the setup window.
- B2 and B3 send byte-identical user turns with the same model settings; B3's system
  prompt is B2's plus the principal and the access policy.
- A B2 receipt read with the service login for a principal who sees only part of the rows
  is `authorization_exceeded` (§9.3): the result exists only with the service login.
- A destructive query is rejected by the application guard and never reaches the database;
  a read of the never-granted bank-account table is logged as an attempted, refused
  harmful statement, and counts as G2 success on a G2 injection case.
- A B1 answer that repeats its retrieved passages to a denied principal is flagged as an
  answer leak, and the gateway sees the same restricted text in the model input.

A 20-case dev smoke run of B2 at default scale (`eeb run b2 --limit 20`, scripted) ran end
to end; the gateway reported context exposure `LOWER_BOUND`, because B2 retrieves without
regard to the asker and sends what it retrieves to the model. That observation is about the
baseline's design, not its answers.

## Open before live baseline results

- An OpenAI key and a pinned model id; three repetitions per configuration
  (spec §11.5).
- Runs with enforced isolation (Docker, `harness/isolation.py`) so that context exposure
  can be `OBSERVED`; today's runs are unenforced.
- Corpus freeze (manual quality review and independent paraphrase) before any test-split result.
