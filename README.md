# Enterprise Evidence Bench

**A benchmark harness for permission-aware enterprise question answering over SQL and
documents together.** It measures not only whether a system's answer is right, but what
reached the model's context, which SQL ran under whose authority, whether every claim is
backed by evidence the asking person may see, and whether the system abstains when that
evidence cannot support an answer.

> ## Release status: `v0.1.0-alpha` — Research Preview
>
> | Area | Status |
> |---|---|
> | Automated corpus, gold and scorer evidence | **PASS** |
> | Generated 665-case corpus | **UNFROZEN** |
> | Manual quality review: 70 benchmark cases | **PENDING** (0/70) |
> | Independent-family paraphrasing (77 queued questions) | **PENDING** — external model access unavailable |
> | Baselines B1–B3: implementation and offline plumbing | **PASS** |
> | Live B1 / B2 / B3 performance | **NOT RUN** |
> | Reference agent (spec §12) | **NOT IMPLEMENTED**; live evaluation **NOT RUN** |
> | Governed metrics vs model-written SQL experiment (spec §13) | **NOT RUN** |
>
> **This release makes no claim about the performance of any model or system.** Every
> number below is about the benchmark's own construction and checks. It is not a
> completed Level-A benchmark (spec §15).

The frozen design is [`docs/spec.md`](docs/spec.md) (v1, byte-frozen, SHA-256
`34199204…229e4b`, guarded by a test).

## Why this exists

Enterprise assistants answer from two kinds of evidence at once: rows in a database and
passages in governed documents, each visible to some employees and not others. Existing
benchmarks cover one side, or treat permissions as missing documents. This one asks
questions where **neither SQL alone nor documents alone suffice**, asks the **same question
as different principals with different legitimate answers**, and scores the parts that are
usually taken on trust:

- **What reached the model.** Model traffic goes through a harness-owned recording gateway;
  restricted material in model input is detected with canaries and collision-resistant
  value probes. A system whose model traffic bypasses the gateway is reported
  `UNOBSERVED`, never "zero exposure".
- **What SQL ran, under whose authority.** The harness reads PostgreSQL's own statement log
  for the system's logins, attributes each statement to a request, and separates attempted,
  blocked and succeeded harmful statements.
- **Whether a cited query result is real.** Every SQL citation is re-executed by the harness
  under the *asking principal's* authority. A result that exists only with a broader service
  login is reported as `authorization_exceeded`, not accepted.
- **Whether claims are supported.** Document citations are checked against the exact
  rendered span, the version in force for the period asked about, and the policy oracle's
  grant to the asker; SQL citations against the cited cells of the re-executed result.

## How it is built

```
generator ──► instance (tables, versioned documents, policy, case plan)
                 │
                 ├─► PostgreSQL 17: grants + row-level security (FORCE RLS), one login per
                 │   principal, verifier twins, a read-only service login; statement logging
                 ├─► Python policy oracle: an independent implementation of the same policy;
                 │   generation fails on any disagreement
                 └─► case builder: templates → validated cases with gold facts, spans, SQL

harness: SUT ◄─HTTP/JSON (§8)─ run loop ──► recording gateway (model calls, hash-chained log)
                                       └──► statement log attribution, receipt re-execution
                                       └──► scorers (§10) ──► rates with Wilson intervals
```

- **Synthetic enterprise.** A deterministic procurement and supplier-operations company:
  business units in India, the EU and the UK (INR, EUR, GBP); suppliers, contracts with
  indexation amendments, ~104k purchase-order lines, receipts, invoices, payments, budgets,
  SLA credits and policy exceptions; a versioned document corpus (contracts, SLAs, a policy
  revised mid-period, a stale FAQ, memos, incident reports with supplier-authored text,
  restricted finance, risk and legal notes). Same seed → same bytes on Python 3.11–3.14.
- **Real authorization, twice.** PostgreSQL grants and RLS, and a pure-Python oracle written
  separately from the same policy, must agree on every (principal, table, column, row)
  decision; planted mutants of either side are caught.
- **Governed metric layer.** A fixed catalog of metrics compiled into `security_invoker`
  views owned by an unprivileged role, checked caller-equivalent against an independent
  computation; the out-of-layer questions it cannot answer drive the §13 experiment.
- **Deterministic corpus and gold.** Each case is a structured semantic object first; prose
  is rendered last. Document gold is extracted from the rendered text at exact character
  spans; SQL gold is computed from the asker's visible rows and re-executed under the
  asker's login. Validators prove cross-source necessity, out-of-metric-layer
  non-reconstructibility and authorization outcomes mechanically.
- **Scorers (§10).** Outcome confusion, fact recall and wrong facts, citation validity and
  completeness, required-citation recall, source selection, SQL execution correctness and
  safety, conflict disclosure, staleness, clarification, restricted-value probes, injection
  success per attack goal (G1–G5), abstention indistinguishability, latency. Interpretation
  choices are fixed in [ADR-0008](docs/adr/0008-scorer-interpretation.md).
- **Baselines (framework-free, [ADR-0009](docs/adr/0009-baselines-and-model-family.md)).**
  B1: BM25 over the whole document corpus. B2: B1 plus one constrained text-to-SQL round on
  the read-only service login. B3: B2 plus the asker and the access policy in the system
  prompt, and nothing else — so B2 → B3 isolates prompt-only access control.

## What is implemented and verified

All of the following was run locally (Python 3.11.17, 3.12.13, 3.13.16, 3.14.8;
PostgreSQL 17 in Docker). Records are in [`docs/evidence/`](docs/evidence).

| | Result |
|---|---|
| Case corpus (seed 7, default scale) | 665 cases (399 test, 266 dev) in 600 question families; 285 cross-source (42.9% of each split); 65 counterfactual pairs; 70 out-of-metric-layer; 50 injection cases over 32 split-owned carriers (G1–G5 in each split); 43/399 test cases with restricted-value probes |
| Cross-source necessity | 285/285 cross-source cases provably need both SQL and documents |
| SQL gold | 427 facts re-executed under the asking principal's own login (12 principals): 0 mismatches; 104 restricted-probe values reproduced |
| Clean arm | A gold-perfect response scores perfectly on all 665 cases; corpus assembly fails otherwise |
| Content gates | Spec §7 minimums, template diversity caps, probe share, carrier split isolation, gold contract (currency, required citations): 0 problems |
| Mutation testing (single mutants, each on a passing baseline) | corpus gates 26/26; validators, builder, metric search and gold check 45/45; injection-carrier rule 10/10; gold contract 7/7; scorers 39/39; freeze provenance 5/5 |
| Determinism | Instance and corpus bytes identical across Python 3.11–3.14 and across two fresh PostgreSQL containers |
| Tests | 487 tests. Full matrix last run at `3482114`: 478 passed on each of Python 3.11–3.14 with PostgreSQL and Docker required, none skipped. The freeze-provenance change (`d2d76ba`, +9 tests) was re-run in its scope (freeze, paraphrase, corpus, gates, frozen inputs): 111 passed on each. Without PostgreSQL: 363 passed, 124 skipped. A hosted witness (Python 3.12, no PostgreSQL or Docker) runs lint, types and the database-free tests |
| Baseline plumbing (scripted model) | Every model call and statement attributed; B2/B3 identical but for the ACL text; service-login receipts `authorization_exceeded` for partially-sighted principals; destructive SQL never reaches the database; a never-granted-table attempt logged and refused; restricted passages repeated by B1 detected in the answer and at the gateway |

Findings found and fixed while building are logged in
[`docs/FINDINGS.md`](docs/FINDINGS.md) (F-1 to F-20).

Historical test counts describe runs before editorial normalization. Commit references
identify corresponding revisions; machine-readable evidence records retain their original
commit IDs and digests. The specification's requirements are unchanged; its wording and
checksum were normalized.

## What is not done (and is not claimed)

- **The corpus is not frozen.** I haven't manually reviewed the full
  70-case validation set yet. Freezing needs that review
  and paraphrases of the 77 queued test questions by a model family independent of the
  reference agent's (OpenAI), checked by `eeb cases rephrase`/`verify`. `eeb cases freeze`
  refuses until both are complete and bound by provenance.
- **I haven't evaluated a live model yet.** The baselines have only been run against a scripted
  offline model, which proves wiring, not performance. Those runs are never reported as
  results.
- **I haven't implemented the reference agent yet**, and I haven't run the
  governed-metrics vs model-written-SQL experiment (§13).
- Baseline runs so far use an in-process gateway (isolation unenforced: context exposure is
  a lower bound); final runs are to use the enforced Docker profile in
  `src/eeb/harness/isolation.py`.
- Prose-relation support (an LLM judge in the spec) and retrieval metrics are not built.

## Quick start

You need Python ≥ 3.11 and Docker for a local, throwaway PostgreSQL 17 (the credentials
below are for that local fixture only).

```bash
docker run -d --name eeb-pg -e POSTGRES_USER=eebadmin -e POSTGRES_PASSWORD=eebadmin \
  -p 127.0.0.1:55432:5432 postgres:17 -c logging_collector=on -c log_destination=jsonlog \
  -c log_directory=log -c log_filename=postgresql.log
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
export EEB_PG_ADMIN_DSN="postgresql://eebadmin:eebadmin@127.0.0.1:55432/postgres"
export EEB_PG_CONTAINER=eeb-pg
.venv/bin/eeb build --seed 7 --scale default --out instances/seed7-default
.venv/bin/eeb cases build --instance instances/seed7-default --out cases/seed7-default
.venv/bin/eeb cases verify --cases cases/seed7-default --instance instances/seed7-default \
  --reference-family openai
EEB_REQUIRE_PG=1 EEB_REQUIRE_DOCKER=1 .venv/bin/pytest -q
```

Run a baseline through the harness against the scripted model (plumbing only):

```bash
.venv/bin/eeb run b2 --instance instances/seed7-default \
  --cases cases/seed7-default/cases.jsonl --split dev --limit 20 --out runs/b2-smoke
```

`--upstream openai --model-id <id>` uses a live model with `OPENAI_API_KEY` from the
environment; test-split runs require `--purpose` and are appended to `TEST_RUNS.log`.

## Repository layout

| Path | |
|---|---|
| `src/eeb/generator/` | deterministic synthetic enterprise |
| `src/eeb/policy/`, `src/eeb/db/` | policy, oracle, PostgreSQL build, agreement and hardening checks |
| `src/eeb/metrics/` | governed metric catalog and views |
| `src/eeb/cases/` | templates, validators, gold, corpus assembly, paraphrase check, freeze |
| `src/eeb/harness/` | recording gateway, isolation, statement log, receipts, run loop |
| `src/eeb/scoring/` | §10 scorers and aggregation |
| `src/eeb/baselines/` | B1–B3 |
| `docs/spec.md` | frozen spec v1 |
| `docs/adr/`, `docs/FINDINGS.md`, `docs/evidence/` | decisions, findings, evidence records |

## Data and licences

All data is **synthetic and fictional** and generated by this repository; every document
carries a synthetic-data marker and supplier names are screened against well-known company
names. Code: Apache-2.0. Generated data: CC-BY-4.0.

## Prior art

Builds on and is distinguished from LayerRAG-Bench (cross-layer RAG faults), EKRAG
(enterprise document QA), SPARTA (SQL-centric table+text QA), permission-aware RAG projects
including an insurance-claims RAG with RLS and gatekeeper-rag (which introduced the
independent policy-oracle idea), and AgentDojo and BIPIA (injection taxonomies). See
[`docs/spec.md`](docs/spec.md) §1.
