# ADR-0004: The Phase 2a case plan fixes corpus shape, not content

Status: accepted (Phase 2a).

`cases/plan.jsonl` fixes:
- case ids (seed-independent);
- base classes;
- counterfactual groups (one permitted X member and one denied A member);
- injection and out-of-metric-layer overlays;
- a group-aware, class-stratified dev/test split ranked by `sha256(seed, id)`.

Question text, principal binding and per-case gold are bound to these slots by Phase 2b
templates.

The frozen §7 arithmetic is checked on the emitted plan file, and generation fails if it is
violated. Each constraint has a red-arm test showing that it can fail. Current shape: 665
cases (dev 266, test 399). X share is 42.9% in each split, and each non-X class has at least
27 test cases.

Two §7 items cannot be checked until cases have content: perturbations (≥ 20% of test) and
human review (≥ 15% of test). They are deferred to Phase 2b, not waived.
