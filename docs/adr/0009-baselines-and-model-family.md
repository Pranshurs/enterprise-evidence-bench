# ADR-0009: Baselines B1–B3 and the model family (status: ACCEPTED, design decision 2026-10-06)

## Baselines are framework-free Python

The benchmark compares system patterns, not framework versions. B1–B3 are small,
inspectable Python implementations; no LangChain or LlamaIndex in Level A. A pinned
framework variant may be added after Level A as an appendix, never as part of acceptance.

- **B1, naive document RAG.** Top-k chunks from the full document corpus into the prompt;
  answers from documents only. No SQL; no principal-aware filtering.
- **B2, SQL + retrieval on the service credential.** B1's retrieval plus a constrained
  text-to-SQL loop on the Mode-S service login. No user-level authorization in the agent.
- **B3, B2 + prompt-only ACL.** B2's code and credential exactly; the asking principal and
  access rules are added to the system prompt with an instruction not to use unauthorized
  data. No enforcement outside the prompt.

B2 → B3 differs only in the ACL instruction.

## Frozen experimental controls

Same exact model id, temperature and settings, output cap and retry policy (through the
gateway) for B1–B3; same question wording; same retrieval implementation and top-k where
applicable; B2 and B3 on the same Mode-S credential; deterministic tool and schema
presentation; no hidden framework memory or retries; model traffic and SQL observed by the
gateway and statement log. Offline scripted-model runs prove plumbing only and are never
reported as baseline performance.

## Model family

- Reference agent and B1–B3: **OpenAI** family, one pinned model id per run.
- Independent rephrasing of the queued questions: **Anthropic** preferred, Gemini
  acceptable; never OpenAI.
- The reference agent is not B3: it is the governed implementation (principal-scoped
  access, metric layer / constrained SQL, verified citations).

Live runs wait until the scorers are closed (mutation campaign, F-18/F-19).
