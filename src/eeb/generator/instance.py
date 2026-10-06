"""Instance generation, generation-time checks and canonical on-disk layout.

Layout (all paths relative to the instance directory; no absolute paths or wall-clock
values are written anywhere):

    INSTANCE.json                  identity, config, per-file digests, summary digests
    policy.yaml                    byte copy of the canonical policy
    tables/<table>.jsonl           rows sorted by primary key
    docs/<doc_id>@v<version>.md    rendered documents
    manifest/documents.jsonl       document metadata (ACL attributes, versions, chunk ids)
    principals.jsonl, assignments.jsonl
    registry/canaries.jsonl, registry/sensitive_values.jsonl
    gold/scenario_facts.jsonl      expected-answer facts with provenance
    cases/plan.jsonl               case skeletons with classes, groups and split
    authorization/outcome.json     oracle outcome digests per (principal, table)
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from eeb import canonical, names
from eeb.cases import plan as caseplan
from eeb.generator import principals as principal_gen
from eeb.generator.core import GENERATOR_VERSION, SYNTHETIC_MARKER, Config, Instance
from eeb.generator.documents import DocumentBuilder
from eeb.generator.domain import DomainBuilder
from eeb.generator.gold import Gold
from eeb.policy import schema as pschema
from eeb.policy.oracle import Oracle
from eeb.schema import BY_NAME, TABLES

FORMAT = "eeb-instance/1"
_NUM = re.compile(r"(?<![0-9.])[0-9]+(?:\.[0-9]+)?(?![0-9]|\.[0-9])")


class GenerationError(RuntimeError):
    pass


def _sort_key(table: str) -> Any:
    pk = BY_NAME[table].pk
    return lambda r: tuple(r[c] for c in pk)


def _cell_text(v: Any) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v)


def generate(cfg: Config) -> tuple[Instance, dict[str, Any]]:
    inst = Instance(cfg)
    db = DomainBuilder(cfg, inst)
    world = db.build()
    DocumentBuilder(inst, world, db.mint).build()
    principal_gen.build(inst)
    inst.scenarios = Gold(inst, world).build()
    inst.injections = sorted(world.injections, key=lambda x: x["incident_id"])
    inst.conflicts = sorted(
        [{"kind": "payment_terms", "contract_id": c.contract_id, "supplier_id": c.supplier_id,
          "system_value": c.payment_terms_days, "document_value": c.doc_payment_terms_days,
          "doc_id": f"DOC-MSA-{c.contract_id}"}
         for c in world.contracts.values() if c.doc_payment_terms_days is not None]
        + [{"kind": "force_majeure", "incident_id": i["incident_id"],
            "supplier_id": i["supplier_id"], "system_value": i["force_majeure"],
            "document_value": i["report_force_majeure"],
            "doc_id": f"DOC-INC-{i['incident_id']}"}
           for i in world.incidents if i["force_majeure"] != i["report_force_majeure"]],
        key=lambda x: (x["kind"], x.get("contract_id") or x.get("incident_id")))
    for name, rows in inst.tables.items():
        rows.sort(key=_sort_key(name))
    missing = {t.name for t in TABLES} - set(inst.tables)
    if missing:
        raise GenerationError(f"tables not generated: {sorted(missing)}")
    return inst, {"world": world}


# ---------------------------------------------------------------------- checks
def _texts(inst: Instance) -> Iterable[tuple[dict[str, Any], str]]:
    """Every text-bearing location: table cells and rendered documents."""
    for t in TABLES:
        for row in inst.tables[t.name]:
            for c in t.columns:
                s = _cell_text(row[c.name])
                if s is not None:
                    yield {"kind": "cell", "table": t.name,
                           "pk": {k: _cell_text(row[k]) for k in t.pk}, "column": c.name}, s
    for d in inst.documents:
        yield {"kind": "doc", "doc_id": d.doc_id, "version": d.version}, d.rendered


def check_canaries(inst: Instance) -> None:
    registered = {c["canary"] for c in inst.canaries}
    if len(registered) != len(inst.canaries):
        raise GenerationError("duplicate canary minted")
    bad = [c for c in registered if not names.is_valid_canary(c)]
    if bad:
        raise GenerationError(f"malformed canaries: {bad[:3]}")
    seen: Counter[str] = Counter()
    for _, text in _texts(inst):
        for m in names.CANARY_RE.finditer(text):
            if m.group(0) not in registered:
                raise GenerationError(f"unregistered canary-shaped token {m.group(0)!r}")
            seen[m.group(0)] += 1
    absent = registered - set(seen)
    if absent:
        raise GenerationError(f"{len(absent)} registered canaries never emitted")


def _sig_digits(s: str) -> int:
    return len(s.replace(".", "").lstrip("0"))


def check_sensitive(inst: Instance) -> None:
    values = [v["value"] for v in inst.sensitive_values]
    if len(values) != len(set(values)):
        raise GenerationError("sensitive values are not unique")
    short = [v for v in values if _sig_digits(v) < 6]
    if short:
        raise GenerationError(f"sensitive values with < 6 significant digits: {short[:3]}")
    allowed: dict[str, dict[str, Any]] = {v["value"]: v["location"] for v in inst.sensitive_values}
    wanted = set(values)
    for loc, text in _texts(inst):
        for m in _NUM.finditer(text):
            tok = m.group(0)
            if tok not in wanted:
                continue
            home = allowed[tok]
            same_doc = (home["kind"] == "doc" and (
                (loc["kind"] == "doc" and loc["doc_id"] == home["doc_id"]) or
                (loc["kind"] == "cell" and loc["table"] == "doc_chunks"
                 and loc["column"] == "text")))
            if same_doc and loc["kind"] == "cell":
                chunk = next(r for r in inst.tables["doc_chunks"]
                             if r["chunk_id"] == loc["pk"]["chunk_id"])
                same_doc = chunk["doc_id"] == home["doc_id"]
            same_cell = home["kind"] == "cell" and loc == home
            if not (same_doc or same_cell):
                raise GenerationError(f"sensitive value {tok} also occurs at {loc}")


def resolve_doc_spans(inst: Instance) -> None:
    docs = {(d.doc_id, d.version): d for d in inst.documents}
    for f in inst.scenarios:
        ref = f.get("doc_ref")
        if ref is None:
            continue
        doc = docs[(ref["doc_id"], ref["version"])]
        start = doc.rendered.find(ref["phrase"])
        if start < 0 or doc.rendered.find(ref["phrase"], start + 1) >= 0:
            raise GenerationError(f"{f['fact_id']}: phrase not found exactly once")
        ref["start"], ref["end"] = start, start + len(ref["phrase"])


def authorization_outcome(inst: Instance, policy: dict[str, Any]) -> dict[str, Any]:
    oracle = Oracle(policy, inst.tables, inst.assignments, inst.config.today)
    full = oracle.outcome([p["principal_id"] for p in inst.principals])
    return summarize_outcome(full)


def summarize_outcome(full: dict[str, dict[str, Any]]) -> dict[str, Any]:
    per = {pid: {t: {"privileged": o["privileged"], "columns": o["columns"],
                     "row_count": len(o["rows"]), "rows_sha256": canonical.digest(o["rows"])}
                 for t, o in tables.items()}
           for pid, tables in full.items()}
    return {"per_principal": per, "digest": canonical.digest(per)}


# ---------------------------------------------------------------------- writing
def render_files(inst: Instance, policy_raw: bytes, plan: list[dict[str, Any]],
                 outcome: dict[str, Any]) -> dict[str, bytes]:
    files: dict[str, bytes] = {"policy.yaml": policy_raw}
    for t in TABLES:
        files[f"tables/{t.name}.jsonl"] = canonical.jsonl(
            {c: row[c] for c in t.column_names} for row in inst.tables[t.name])
    for d in inst.documents:
        files[f"docs/{d.key}.md"] = d.rendered.encode("utf-8")
    files["manifest/documents.jsonl"] = canonical.jsonl(
        {"doc_id": d.doc_id, "version": d.version, "title": d.title,
         "classification": d.classification, "scope_category": d.scope_category,
         "scope_bu": d.scope_bu, "effective_from": d.effective_from,
         "effective_to": d.effective_to, "supersedes": d.supersedes, "authority": d.authority,
         "path": f"docs/{d.key}.md", "sha256": canonical.sha256_text(d.rendered),
         "chunk_ids": [r["chunk_id"] for r in inst.tables["doc_chunks"]
                       if r["doc_id"] == d.doc_id and r["version"] == d.version]}
        for d in inst.documents)
    files["principals.jsonl"] = canonical.jsonl(inst.principals)
    files["assignments.jsonl"] = canonical.jsonl(inst.assignments)
    files["registry/canaries.jsonl"] = canonical.jsonl(
        sorted(inst.canaries, key=lambda c: c["canary"]))
    files["registry/sensitive_values.jsonl"] = canonical.jsonl(
        sorted(inst.sensitive_values, key=lambda v: v["value"]))
    files["gold/scenario_facts.jsonl"] = canonical.jsonl(
        sorted(inst.scenarios, key=lambda f: f["fact_id"]))
    files["cases/plan.jsonl"] = canonical.jsonl(plan)
    files["registry/injections.jsonl"] = canonical.jsonl(inst.injections)
    files["registry/conflicts.jsonl"] = canonical.jsonl(inst.conflicts)
    files["authorization/outcome.json"] = (canonical.dumps(outcome) + "\n").encode()
    return files


def build_instance_files(cfg: Config) -> dict[str, bytes]:
    """Generate, check and render every artifact. Raises GenerationError on any failure."""
    inst, _ = generate(cfg)
    policy_raw = pschema.policy_bytes()
    policy = pschema.load_policy(policy_raw)
    pschema.validate_principals(inst.principals, inst.assignments, policy)
    check_canaries(inst)
    check_sensitive(inst)
    resolve_doc_spans(inst)
    plan = caseplan.build_plan(cfg.seed)
    violations = caseplan.check_constraints(plan)
    if violations:
        raise GenerationError("case plan violates frozen constraints: " + "; ".join(violations))
    outcome = authorization_outcome(inst, policy)
    files = render_files(inst, policy_raw, plan, outcome)
    digests = {path: canonical.sha256_bytes(b) for path, b in sorted(files.items())}
    meta = {
        "format": FORMAT, "generator_version": GENERATOR_VERSION,
        "config": cfg.as_record(), "synthetic": SYNTHETIC_MARKER,
        "policy_version": policy["policy_version"],
        "counts": {t.name: len(inst.tables[t.name]) for t in TABLES}
        | {"documents": len(inst.documents), "principals": len(inst.principals),
           "canaries": len(inst.canaries), "sensitive_values": len(inst.sensitive_values),
           "gold_facts": len(inst.scenarios), "cases": len(plan)},
        "case_plan": caseplan.summary(plan),
        "authorization_outcome_digest": outcome["digest"],
        "files": digests,
        "instance_digest": canonical.digest(digests),
    }
    files["INSTANCE.json"] = (canonical.dumps(meta) + "\n").encode()
    return files


def write_files(files: dict[str, bytes], out: Path) -> None:
    for rel, data in sorted(files.items()):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
