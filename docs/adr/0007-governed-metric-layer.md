# ADR-0007: Governed metric layer (spec §4.4, §12, §13)

Status: accepted (Phase 2b).

## Non-negotiable property: never a privileged bypass around RLS

- **Views, not functions.** Metrics compile to one SELECT over one view in schema `eeb_m`.
  Every view is `security_invoker = true`, so the **caller's** RLS policies and column
  grants apply. A caller missing a column the view reads gets permission denied (fails
  closed).
- **Unprivileged owner.** Views and schema are owned by `<ns>_metric_owner`: NOLOGIN, not
  superuser, not BYPASSRLS, and no privilege on any base table. If invoker security were
  ever lost, a query would fail closed rather than run with an owner's rights.
- **No SECURITY DEFINER** functions exist outside `pg_catalog` and `information_schema`.
- **The build gate proves all of this every time** (`metric_layer_checks`):
  - `eeb_m` holds exactly the declared views;
  - each is `security_invoker`, owned by the metric owner;
  - the owner has no privileged attributes and no table privilege;
  - there are no SECURITY DEFINER functions.

## Evidence

- **Caller equivalence.** For 11 principals × 10 metric configurations, every result run as
  the principal equals an independent Python computation over exactly the rows and columns
  the policy oracle grants that principal. That covers "denied" for principals without the
  needed grants (`tests/test_metric_layer.py::test_metric_layer_is_caller_equivalent`).
- **View-owner red arm.** The metric SQL is mathematically correct, and the case is chosen so
  that executing as the view owner includes exactly one unauthorized row (a cross-unit
  invoice that the PO's business-unit clerk may not see):
  - *Owner-rights variant* (definer views owned by the superuser admin): the result leaks
    exactly that invoice's amount, and the comparison with the reference fails.
  - *Unprivileged definer variant*: permission denied, so it fails closed.
  - The build gate flags both (`not security_invoker`).

## Expressiveness boundary (keeps the §13 experiment meaningful)

- **Fixed catalog.** `data/metrics.yaml` is checksum-frozen, with seven predeclared
  metrics: invoiced amount, ordered amount, effective unit cost, PO count, raw on-time
  delivery %, rejection rate %, budget amount.
- **Fixed SQL shape.** The compiler accepts only:
  - a catalog metric;
  - its declared dimensions and filters;
  - filter values matching `[A-Za-z0-9_-]+`, bound as parameters.

  Each metric compiles to exactly one fixed SELECT shape. No predicates, joins, columns or
  expressions come from the caller, so the layer is not a generic view or a route to
  arbitrary SQL.
- **Out-of-layer material is absent from every metric view** (tested):
  - delay causes and incidents (SLA-adjusted on-time delivery and service credits);
  - the contract price schedule (price variance);
  - exceptions and contract ids (off-contract compliance);
  - FX rates, payments and payment terms;
  - documents.

  Questions that need these are out-of-layer by construction, not by labelling.
- **Separation from generated SQL.** The constrained model-written-SQL arm (M+G) will use
  a separate curated schema. It does not widen `eeb_m`.

## Substrate closure including the metric layer

`docs/evidence/phase2b/substrate_closure_metric_layer.json`: two fresh containers.
- Instances are byte-identical (`5e3133f8…`); the authorization digest is `b539e3dc…` on
  database, oracle, recorded and verifier twins; the service digest is `901db369…`.
- 0 disagreements, 0 twin or service problems, 0 hardening, logging or metric-layer
  problems; 29 of 29 gold facts. A verifies on B.
