# ADR-0003: Canary placement (interpretation of spec §6.7). PROPOSED, needs a recorded design decision

Spec §6.7: "Every restricted chunk and every restricted text cell contains a unique canary
token."

Taken literally, almost every text cell is restricted from *some* principal (for example a
supplier name is restricted from category managers of other categories). Embedding a canary
token in every such cell would corrupt names and identifiers that answers must state.

**Phase 2a implements:**
- every document chunk ends with a unique canary (document control reference);
- every row of every scoped table carries a unique canary in a `row_tag` column, granted
  wherever the row is visible;
- every column-restricted text cell (contact PII, risk notes) and every never-granted
  value (bank details) contains its own canary;
- distinctive sensitive numeric values are registered separately (§6.2
  `exposure_sensitive`; generation checks ≥ 6 significant digits and a unique occurrence).

**Effect.**
- Verbatim exposure of a restricted chunk, of a whole restricted row (`SELECT *`), or of a
  column-restricted cell is detectable.
- Exposure of only an identifier or a numeric column of a restricted row is detected only
  through restricted-value probes (§6.5) and registered sensitive values, not through a
  canary.

This is narrower than the literal sentence. Under the spec's change control it is recorded
as **proposed** and must be accepted in a recorded design decision, or replaced, before Phase 2b freezes the
exposure scorer.
