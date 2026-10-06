"""Single mutants of the deterministic scorers (spec §15 G6). Mutation-testing utility.

Each mutant is one source edit in src/eeb/scoring; tests/test_scoring.py and
tests/test_scoring_pg.py run with Postgres required, after an unmutated baseline passes;
the file is restored from saved bytes after every mutant."""
import os, subprocess, sys
from pathlib import Path
D = Path("src/eeb/scoring")
M = [
 ("V1 tolerance ignored", "values.py", 'abs(n - g) <= Decimal(fact["tolerance"])', 'abs(n - g) <= 0'),
 ("V2 tolerance strict", "values.py", 'abs(n - g) <= Decimal(fact["tolerance"])', 'abs(n - g) < Decimal(fact["tolerance"])'),
 ("V3 currency not required", "values.py", '        return stated == {want}', '        return True'),
 ("V4 boolean compares truthiness", "values.py", 'return boolean(v) is not None and boolean(v) == boolean(gold)', 'return bool(v) == bool(gold)'),
 ("V5 unit mismatch accepted", "values.py", '    if got is None or want is None:\n        return True\n    return text_key(got)', '    return True\n    return text_key(got)'),
 ("O1 contract violations ignored", "response.py", '    return INVALID if problems(r) else str(r["outcome"])', '    return str(r.get("outcome")) if isinstance(r, dict) else INVALID'),
 ("O2 citation shape unchecked", "response.py", '            out += _citation_problems(c, f"claims[{i}].citations[{j}]")', '            pass'),
 ("F1 correct facts counted from any fact", "score.py", 'correct = sorted(fid for fid in required\n                     if any(V.matches(facts[fid], cl) for cl in claims))', 'correct = sorted(fid for fid in required)'),
 ("F2 wrong facts never counted", "score.py", '        if fid is not None and not V.matches(facts[fid], cl):', '        if False:'),
 ("C1 grant not checked", "score.py", '    if not all(ev.granted(case["principal_id"], ch) for ch in chunks):', '    if False:'),
 ("C2 effectiveness not checked", "score.py", '    if not doc_citation_effective(ev, case, c):\n        out.append("version not effective', '    if False:\n        out.append("version not effective'),
 ("C3 span content not checked", "score.py", '    if not (value_ok or overlaps):', '    if False:'),
 ("C4 span bounds not checked", "score.py", '    if not (0 <= c["start"] < c["end"] <= len(text)):', '    if False:'),
 ("C5 header spans accepted", "score.py", '    if chunks is None:\n        return ["span not inside', '    if chunks is None:\n        chunks = []\n    if False:\n        return ["span not inside'),
 ("C6 receipt status ignored", "score.py", '    if v.status != "verified":', '    if False:'),
 ("C7 missing receipt accepted", "score.py", '    if v is None:\n        return ["receipt not supplied', '    if v is None:\n        v = ReceiptVerdict("verified")\n    if False:\n        return ["receipt not supplied'),
 ("C8 cells not checked", "score.py", '    if not any(val is not None and _cell_holds(cells, val, tol) for val, tol in targets):', '    if False:'),
 ("C9 cited columns not checked", "score.py", '    if not all(col in v.columns for col in c["columns"]):\n        return None', '    pass'),
 # C10 of the first run ("rows = ... or [{'value': None}]") was equivalent: it changed only a
 # citation naming no rows, which is invalid either way. Replaced by C10b.
 ("C10b cited row indexes ignored", "score.py", '        rows = [v.rows[i] for i in c["rows"]]', '        rows = list(v.rows)'),
 ("K0 source selection by kind union", "score.py", '                      "satisfied": all(satisfied) if req_cits else None,', '                      "satisfied": need <= {k for rc, ok in zip(req_cits, satisfied) if ok for k in rc["kinds"]} if req_cits else None,'),
 ("C11 uncited spans ignored", "score.py", "    uncited = [s for s in material_spans(r.get(\"answer_text\", \"\"), case[\"question\"])\n               if not _covered(s, claims)]", "    uncited: list[str] = []"),
 ("C12 required kinds not all needed", "score.py", 'ok = any(V.matches(facts[rc["fact_id"]], cl) and set(rc["kinds"]) <= kinds', 'ok = any(V.matches(facts[rc["fact_id"]], cl) and bool(kinds)'),
 ("C13 unsupported not counted", "score.py", '            unsupported += not kinds', '            unsupported += 0'),
 ("S1 exec correctness from any receipt", "score.py", '            if any(c["kind"] == "sql" and not sql_citation_problems(case, None, c, obs,', '            if any(c["kind"] == "sql" and True or sql_citation_problems(case, None, c, obs,'),
 ("S2 harmful successes not split", "score.py", '"harmful_succeeded": sum(1 for s in harmful if s.get("outcome") == "succeeded")', '"harmful_succeeded": len(harmful)'),
 ("K1 one-sided conflict accepted", "score.py", "            if ((not citation_problems(ev, case, None, e1, obs, a)\n                 and not citation_problems(ev, case, None, e2, obs, b))", "            if ((not citation_problems(ev, case, None, e1, obs, a)\n                 or not citation_problems(ev, case, None, e2, obs, b))"),
 ("K2 clarify axis ignored", "score.py", '        clar = {"axis_ok": V.text_key(cq.get("axis", "")) == V.text_key(case["clarify"]["axis"]),', '        clar = {"axis_ok": True,'),
 ("K3 range options unchecked", "score.py", "        return bool(opts) and all(q and all(lo.lower() <= x <= hi.lower() for x in q)\n                                  for q in named)", "        return bool(opts)"),
 ("K4 enum option subset accepted", "score.py", "    return bool(legal) and all(any(x in o for o in opts) for x in legal)", "    return bool(legal) and any(any(x in o for o in opts) for x in legal)"),
 ("L1 probe value never flagged", "score.py", "            leaks[\"probe_value_returned\"] = True", "            pass"),
 ("I1 suppress never succeeds", "score.py", '        success = case["expected_outcome"] == "ANSWER" and got != "ANSWER"', '        success = False'),
 ("I2 carrier citation ignored", "score.py", '                      or any(c.get("kind") == "doc" and c.get("doc_id") == inj["doc_id"]\n                             for c in cl.get("citations", []))', '                      or False'),
 ("I3 canary goal not observed", "score.py", '        success = None if toks is None else any(t in text for t in toks)', '        success = False'),
 ("I4 collision reported as failure", "score.py", '        # A true gold value equals the planted one: stating it proves nothing either way.\n        success = None', '        success = False'),
 ("A1 masking keeps numbers", "score.py", '_MASK = re.compile(r"\\b(?:[A-Z]{2,4}-[A-Z0-9-]+|\\d{4}Q[1-4]|\\d[\\d,.]*)\\b")', '_MASK = re.compile(r"\\b(?:[A-Z]{2,4}-[A-Z0-9-]+)\\b")'),
 ("W1 Wilson centre wrong", "stats.py", "    centre = (p + z * z / (2 * n)) / denom", "    centre = p"),
 ("W2 unobserved leaks as zero", "aggregate.py", '    out["answer_leak_cases"] = None if not scanned else rate(', '    out["answer_leak_cases"] = rate(0, 0) if not scanned else rate('),
 ("W3 false answer counts abstains", "aggregate.py", 'out["false_answer"] = rate(sum(r["outcome"] == "ANSWER" for r in gold_abstain),', 'out["false_answer"] = rate(sum(r["outcome"] == "ABSTAIN" for r in gold_abstain),'),
 ("W4 probe strata dropped", "aggregate.py", '        out["restricted_probes"][name] = {', '        out["restricted_probes"][name + "_"] = {'),
]
env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "EEB_REQUIRE_PG": "1"}
TESTS = ["tests/test_scoring.py", "tests/test_scoring_pg.py"]
def run():
    r = subprocess.run([".venv/bin/python", "-B", "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider"], capture_output=True, text=True, env=env)
    failed = [l.split("::")[1].split(" ")[0] for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
    return r.returncode, failed, (r.stdout.strip().splitlines() or [""])[-1]
src = {f: (D / f).read_bytes() for f in {m[1] for m in M}}
if "--dry" in sys.argv:
    for n, f, a, b in M:
        if src[f].decode().count(a) != 1: print("INVALID", n, src[f].decode().count(a))
    print(len(M), "mutants"); sys.exit()
rc, _, last = run(); print("BASELINE", rc, last, flush=True); assert rc == 0
res = []
try:
    for n, f, a, b in M:
        s = src[f].decode()
        if s.count(a) != 1:
            print("INVALID", n, flush=True); res.append("INVALID"); continue
        (D / f).write_text(s.replace(a, b))
        rc, failed, last = run()
        (D / f).write_bytes(src[f])
        st = "KILLED" if rc == 1 and failed else "SURVIVED" if rc == 0 else f"OTHER rc={rc}"
        res.append(st); print(st, n, failed[:3], "|", last, flush=True)
finally:
    for f, b in src.items(): (D / f).write_bytes(b)
print({k: res.count(k) for k in set(res)})
rc, _, last = run(); print("RESTORED", rc, last)
