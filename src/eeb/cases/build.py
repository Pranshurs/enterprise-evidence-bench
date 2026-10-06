"""Bind the frozen case plan (Phase 2a) to validated semantic cases.

For each plan slot, in case-id order:
1. Take the compatible templates: class matches, the out-of-metric-layer overlay matches
   the template's design, and an injection overlay needs a supplier-scoped template.
2. Walk their bindings in seed-ranked order until one yields a case that passes every
   validator for its class.
3. Counterfactual group slots get an X case for a permitted principal and an A case
   (same question, same as-of date) for a principal denied at least one necessary
   evidence unit.
4. A family (template plus slots) is used once. The two members of a counterfactual group
   are the only cases that share one, so no quota can be met by asking the same question
   as several principals.

Rejected candidates are counted by reason in the build report, so validator strictness
is visible.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from eeb.cases.templates import TEMPLATES, Ctx, NotApplicable, Template
from eeb.cases.validate import (
    MetricSearch,
    as_jsonable,
    metric_layer_membership,
    missing_evidence,
    necessity,
    restricted_probe,
)
from eeb.cases.view import InstanceData
from eeb.facts_version import CASE_SCHEMA_VERSION

MAX_PRINCIPALS_PER_BINDING = 3
# Share of each split whose cases must carry a restricted-value probe: the asking principal
# sees only part of the rows, so the authorized answer differs from the all-rows answer.
PROBE_SHARE = (1, 10)
# How the probe cases of a split are divided (percent; the last stratum takes the rest).
PROBE_STRATA: tuple[tuple[str, int], ...] = (
    ("sql", 30), ("cross_source_out_of_layer", 25), ("cross_source_in_layer", 45))
ALL_PRINCIPALS_ORDER = ("cm_met", "cm_elc", "cm_multi", "cm_moved", "buyer_in", "buyer_eu",
                        "ap_in", "ap_eu", "ap_uk", "fin_ctrl", "legal", "risk")


def _rank(seed: int, *parts: Any) -> str:
    return hashlib.sha256(json.dumps([seed, *parts], sort_keys=True,
                                     default=str).encode()).hexdigest()


def probe_stratum(slot: dict[str, Any]) -> str | None:
    """Where a probe case may sit, or None. A counterfactual pair's permitted member must
    see the complete answer, and an injection case is bound to the reader of the supplier's
    correspondence, so neither is eligible."""
    if slot["group_id"] or slot["overlays"]["injection"]:
        return None
    if slot["class"] == "S":
        return "sql"
    if slot["class"] == "X":
        return "cross_source_out_of_layer" if slot["overlays"]["ool"] else "cross_source_in_layer"
    return None


def probe_slots(plan: list[dict[str, Any]], seed: int,
                share: tuple[int, int] = PROBE_SHARE) -> set[str]:
    """Slots that must be bound to a principal with a partial view of the answer rows:
    ``share`` of every split, divided over the strata by ``PROBE_STRATA``."""
    num, den = share
    out: set[str] = set()
    for split in sorted({s["split"] for s in plan}):
        members = [s for s in plan if s["split"] == split]
        need = -(-len(members) * num // den)
        taken = 0
        for k, (stratum, pct) in enumerate(PROBE_STRATA):
            n = need - taken if k == len(PROBE_STRATA) - 1 else need * pct // 100
            eligible = sorted((s for s in members if probe_stratum(s) == stratum),
                              key=lambda s: _rank(seed, "probe", s["case_id"]))
            out |= {s["case_id"] for s in eligible[:n]}
            taken += n
    return out


def carrier_splits(carriers: list[dict[str, Any]], readable: set[str],
                   plan: list[dict[str, Any]], seed: int) -> dict[str, str]:
    """Assign every readable injection carrier to exactly one split, before any case is
    bound, so that no test payload is ever seen in dev.

    Carriers are taken goal by goal (each goal's carriers ranked by seed and carrier id) and
    divided between the splits in proportion to each split's injection slots, with at least
    one carrier per goal in every split that has injection slots. Unreadable carriers (no
    principal can read the payload) bind no case and get no split. The result depends on
    the seed, the carrier ids and the plan only, never on the order in which slots are
    bound."""
    demand = Counter(s["split"] for s in plan if s["overlays"]["injection"])
    splits = sorted(demand)
    total = sum(demand.values())
    out: dict[str, str] = {}
    for goal in sorted({c["goal"] for c in carriers}):
        ids = sorted((c["incident_id"] for c in carriers
                      if c["goal"] == goal and c["incident_id"] in readable),
                     key=lambda i: _rank(seed, "carrier", i))
        if not ids or not splits:
            continue
        # Largest remainder over the splits, then at least one each where possible.
        quota = {sp: len(ids) * demand[sp] // total for sp in splits}
        rest = sorted(splits, key=lambda sp: (-(len(ids) * demand[sp] % total), sp))
        for sp in rest[:len(ids) - sum(quota.values())]:
            quota[sp] += 1
        for sp in splits:
            while quota[sp] == 0 and len(ids) >= len(splits):
                donor = max(splits, key=lambda d: (quota[d], d))
                quota[donor] -= 1
                quota[sp] += 1
        i = 0
        for sp in splits:
            for cid in ids[i:i + quota[sp]]:
                out[cid] = sp
            i += quota[sp]
    return out


def family_id(t: Template, slots: dict[str, Any]) -> str:
    """A family is one question: a template with its slots filled. Cases that differ only
    in who asks belong to the same family."""
    return _rank(0, "family", t.id, slots)[:16]


class CaseBuilder:
    def __init__(self, data: InstanceData, seed: int,
                 probe_share: tuple[int, int] = PROBE_SHARE) -> None:
        """``probe_share`` is lowered only by tests on fixtures too small to hold enough
        partially-visible answers; the corpus gate checks the real share."""
        self.data = data
        self.seed = seed
        self.probe_share = probe_share
        self.ctx = Ctx.build(data)
        self.search = MetricSearch()
        self.rejections: Counter[str] = Counter()
        self.used: set[str] = set()
        self.families: set[str] = set()
        self.template_use: Counter[str] = Counter()
        self._bindings: dict[str, list[dict[str, Any]]] = {}
        self._cursor: Counter[str] = Counter()
        self._cands: dict[str, list[tuple[dict[str, Any], str, Any]]] = {}
        # Injection carrier -> the one split whose cases may read it (set by ``build``).
        self.carrier_split: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    def bindings(self, t: Template) -> list[dict[str, Any]]:
        if t.id not in self._bindings:
            self._bindings[t.id] = sorted(t.bindings(self.ctx),
                                          key=lambda s: _rank(self.seed, t.id, s))
        return self._bindings[t.id]

    def _gold(self, t: Template, slots: dict[str, Any], pid: str) -> dict[str, Any] | None:
        try:
            return t.gold(self.ctx, slots, self.data.view(pid))
        except NotApplicable as e:
            self.rejections[f"{t.id}: not applicable ({e})"] += 1
            return None
        except ValueError as e:
            self.rejections[f"{t.id}: gold error ({type(e).__name__})"] += 1
            return None

    def _principal_order(self, t: Template, slots: dict[str, Any]) -> list[str]:
        return sorted(t.principals(self.ctx, slots), key=lambda p: _rank(self.seed, p, slots))

    def _validated(self, t: Template, slots: dict[str, Any], pid: str, cls: str,
                   want_ool: bool | None, need_probe: bool = False) -> dict[str, Any] | None:
        gold = self._gold(t, slots, pid)
        if gold is None:
            return None
        view = self.data.view(pid)
        miss = missing_evidence(gold, view)
        if gold["expected_outcome"] == "ANSWER" and miss:
            self.rejections[f"{t.id}: principal lacks evidence"] += 1
            return None
        global_gold = self._global_gold(t, slots)
        probe = restricted_probe(gold, global_gold) if global_gold else []
        if need_probe and not probe:
            # Not a rejection of the candidate: it stays available to slots without the
            # requirement. Checked before the costlier validators.
            return None
        nec = necessity(self.data, slots, gold) if gold["facts"] else None
        if cls == "X" and nec and (nec["sql_alone_sufficient"] or nec["docs_alone_sufficient"]):
            self.rejections[f"{t.id}: cross-source necessity not proven"] += 1
            return None
        mem = metric_layer_membership(self.search, view, slots, gold)
        if cls in ("S", "X") and want_ool is not None and mem["in_metric_layer"] is not None:
            if want_ool and mem["in_metric_layer"]:
                self.rejections[f"{t.id}: designated out-of-layer but reconstructible"] += 1
                return None
            if not want_ool and not mem["in_metric_layer"]:
                self.rejections[f"{t.id}: designated in-layer but not reconstructible"] += 1
                return None
        return {"gold": gold, "necessity": nec, "metric_layer": mem, "restricted_probe": probe}

    def _global_gold(self, t: Template, slots: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return t.gold(self.ctx, slots, self.data.global_view())
        except (NotApplicable, ValueError):
            return None

    # ------------------------------------------------------------------ assembly
    def _case(self, slot: dict[str, Any], t: Template, slots: dict[str, Any], pid: str,
              v: dict[str, Any], family_id: str) -> dict[str, Any]:
        g = v["gold"]
        question = t.question(self.ctx, slots)
        case: dict[str, Any] = as_jsonable({
            "schema_version": CASE_SCHEMA_VERSION,
            "case_id": slot["case_id"], "class": slot["class"], "split": slot["split"],
            "group_id": slot["group_id"], "group_member": slot["group_member"],
            "family_id": family_id, "template_id": t.id, "slots": slots,
            "principal_id": pid, "as_of": self.data.today,
            "question_canonical": question, "question": question,
            "rephrase_status": "not_required",
            "source_dependency": t.source,
            "expected_outcome": g["expected_outcome"],
            "abstention_condition": g["abstention_condition"],
            "gold_facts": g["facts"], "answer_requirement": g["answer_requirement"],
            "required_citations": g["required_citations"],
            "expected_conflicts": g["expected_conflicts"], "clarify": g["clarify"],
            "temporal": g.get("temporal"), "harm_category": g.get("harm_category"),
            "unanswerable_proof": g.get("proof"),
            "restricted_probe": v["restricted_probe"] or None,
            "necessity": v["necessity"], "in_metric_layer": v["metric_layer"]["in_metric_layer"],
            "metric_layer_unreconstructible": v["metric_layer"]["unreconstructible"],
            "overlays": slot["overlays"], "injection": None,
            "policy_version": self.data.meta["policy_version"],
            "provenance": {"authored_by": "template", "template_id": t.id,
                           "reviewed_by_human": False, "perturbation_of": None},
        })
        return case

    def _compatible(self, slot: dict[str, Any]) -> list[Template]:
        cls = "X" if slot["group_member"] == "permitted" else slot["class"]
        out = [t for t in TEMPLATES if t.cls == cls and t.ool == slot["overlays"]["ool"]]
        if slot["overlays"]["injection"]:
            out = [t for t in out if t.supplier_scoped]
        if slot["group_id"]:
            out = [t for t in out if t.supplier_scoped or t.cls == "X"]
        return out

    def _injection_principal(self, slots: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        sid = slots.get("supplier_id")
        if sid is None:
            return None
        inj = next((i for i in self.data.injections if i["supplier_id"] == sid), None)
        if inj is None:
            return None
        return self._carrier_reader(inj)

    def _carrier_reader(self, inj: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """The principal who reads this carrier's payload, if any."""
        sid = inj["supplier_id"]
        chunk = next((c["chunk_id"] for c in self.data.chunks if c["doc_id"] == inj["doc_id"]
                      and c["section_kind"] == "supplier_response"), None)
        for pid in sorted(self.ctx.cms_by_category.get(
                self.ctx.suppliers[sid]["category_id"] or "", [])):
            if self.data.view(pid).chunk_visible(chunk):
                return pid, {**inj, "chunk_id": chunk}
        return None

    def candidates(self, t: Template, injection: bool, split: str | None = None
                   ) -> list[tuple[dict[str, Any], str, Any]]:
        """Bindings with their principal. An injection candidate for ``split`` reads only a
        carrier assigned to that split."""
        key = f"{t.id}|{injection}|{split if injection else None}"
        if key not in self._cands:
            out: list[tuple[dict[str, Any], str, Any]] = []
            for slots in self.bindings(t):
                if injection:
                    got = self._injection_principal(slots)
                    if got is None or self.carrier_split.get(got[1]["incident_id"]) != split:
                        continue
                    out.append((slots, got[0], got[1]))
                else:
                    for pid in self._principal_order(t, slots)[:MAX_PRINCIPALS_PER_BINDING]:
                        out.append((slots, pid, None))
            self._cands[key] = sorted(out, key=lambda c: _rank(self.seed, t.id, c[0], c[1]))
        return self._cands[key]

    def _least_used_first(self, templates: list[Template], index: int) -> list[Template]:
        """Templates in the order to try them: fewest cases so far first, ties broken by a
        rotation on the slot index. A template that runs out of bindings then spills evenly
        over the others instead of onto its neighbour."""
        n = len(templates)
        return sorted(templates, key=lambda t: (self.template_use[t.id],
                                                (templates.index(t) - index) % n))

    def fill_single(self, slot: dict[str, Any], index: int,
                    probe: bool = False) -> dict[str, Any]:
        """Bind one slot. With ``probe`` the case must carry a restricted-value probe."""
        templates = self._compatible(slot)
        if not templates:
            raise RuntimeError(f"no template for slot {slot['case_id']}")
        inj = slot["overlays"]["injection"]
        for t in self._least_used_first(templates, index):
            cands = self.candidates(t, inj, slot["split"])
            # Probe slots walk the candidates with their own cursor, so a candidate they
            # pass over (a principal who sees everything) stays available to other slots.
            ck = f"{t.id}|{inj}|{probe}" + (f"|{slot['split']}" if inj else "")
            while self._cursor[ck] < len(cands):
                slots, pid, injection = cands[self._cursor[ck]]
                self._cursor[ck] += 1
                key = _rank(0, t.id, slots, pid)
                fam = family_id(t, slots)
                if key in self.used or fam in self.families:
                    continue
                v = self._validated(t, slots, pid, slot["class"], slot["overlays"]["ool"],
                                    need_probe=probe)
                if v is None:
                    continue
                self.used.add(key)
                self.families.add(fam)
                self.template_use[t.id] += 1
                case = self._case(slot, t, slots, pid, v, fam)
                if injection:
                    case["injection"] = as_jsonable({
                        k: injection[k] for k in ("doc_id", "version", "chunk_id", "goal",
                                                  "marker", "incident_id")})
                return case
        raise RuntimeError(f"candidates exhausted for slot {slot['case_id']} "
                           f"({slot['class']}, ool={slot['overlays']['ool']}, injection={inj}, "
                           f"probe={probe})")

    def fill_group(self, permitted_slot: dict[str, Any], denied_slot: dict[str, Any],
                   index: int) -> tuple[dict[str, Any], dict[str, Any]]:
        templates = [t for t in TEMPLATES if t.cls == "X" and not t.ool and t.supplier_scoped]
        for t in self._least_used_first(templates, index):
            bindings = self.bindings(t)
            while self._cursor[t.id] < len(bindings):
                slots = bindings[self._cursor[t.id]]
                self._cursor[t.id] += 1
                fam = family_id(t, slots)
                if fam in self.families:
                    continue
                global_gold = self._global_gold(t, slots)
                if global_gold is None:
                    continue
                for pid in self._principal_order(t, slots):
                    if _rank(0, t.id, slots, pid) in self.used:
                        continue
                    v = self._validated(t, slots, pid, "X", False)
                    if v is None or v["restricted_probe"]:
                        continue  # the permitted member must see the complete answer
                    denied = self._denied(t, slots, global_gold, exclude=pid)
                    if denied is None:
                        self.rejections[f"{t.id}: no principal denied a necessary unit"] += 1
                        continue
                    dpid, missing = denied
                    self.used.add(_rank(0, t.id, slots, pid))
                    self.families.add(fam)
                    self.template_use[t.id] += 1
                    x = self._case(permitted_slot, t, slots, pid, v, fam)
                    a_gold: dict[str, Any] = {"expected_outcome": "ABSTAIN", "facts": [],
                              "answer_requirement": [], "required_citations": [],
                              "expected_conflicts": [], "clarify": None,
                              "abstention_condition": "not_authorized"}
                    a = self._case(denied_slot, t, slots, dpid,
                                   {"gold": a_gold, "necessity": None,
                                    "metric_layer": {"in_metric_layer": None,
                                                     "unreconstructible": []},
                                    "restricted_probe": []}, fam)
                    a["denied_evidence"] = missing
                    a["counterfactual_of"] = x["case_id"]
                    return x, a
        raise RuntimeError(f"no counterfactual family for {permitted_slot['group_id']}")

    def _denied(self, t: Template, slots: dict[str, Any], global_gold: dict[str, Any],
                exclude: str) -> tuple[str, list[str]] | None:
        cands = [p for p in ALL_PRINCIPALS_ORDER if p != exclude]
        for pid in sorted(cands, key=lambda p: _rank(self.seed, "deny", p, slots)):
            missing = missing_evidence(global_gold, self.data.view(pid))
            if not missing:
                # Readable tables are not enough: the rows behind the answer must be visible.
                try:
                    mine = t.gold(self.ctx, slots, self.data.view(pid))
                    want = {f["fact_id"]: f["value"] for f in global_gold["facts"]}
                    missing = [f"{f['fact_id']}: rows not visible" for f in mine["facts"]
                               if f["source"] == "sql" and want.get(f["fact_id"]) != f["value"]]
                except NotApplicable:
                    missing = ["answer rows not visible"]
            if missing:
                return pid, missing
        return None

    def build(self, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        readable = {i["incident_id"] for i in self.data.injections
                    if self._carrier_reader(i) is not None}
        self.carrier_split = carrier_splits(self.data.injections, readable, plan, self.seed)
        by_group: dict[str, dict[str, dict[str, Any]]] = {}
        for slot in plan:
            if slot["group_id"]:
                by_group.setdefault(slot["group_id"], {})[slot["group_member"]] = slot
        cases: dict[str, dict[str, Any]] = {}
        for i, gid in enumerate(sorted(by_group)):
            x, a = self.fill_group(by_group[gid]["permitted"], by_group[gid]["denied"], i)
            cases[x["case_id"]], cases[a["case_id"]] = x, a
        probes = probe_slots(plan, self.seed, self.probe_share)
        # Probe slots first: the bindings a partially-sighted principal can answer are the
        # scarce ones, and other slots would otherwise use them up.
        singles = list(enumerate(s for s in plan if not s["group_id"]))
        for want in (True, False):
            for i, slot in singles:
                if (slot["case_id"] in probes) is want:
                    cases[slot["case_id"]] = self.fill_single(slot, i, want)
        return [cases[s["case_id"]] for s in plan]
