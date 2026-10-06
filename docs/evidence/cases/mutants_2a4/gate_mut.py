"""Single mutants of the corpus freeze gates (mutation-testing utility)."""
import subprocess, sys, os
from pathlib import Path
F = Path("src/eeb/cases/corpus.py")
ORIG = F.read_bytes()
M = [
 ("G1 case minimum off", "    if len(cases) < SPEC_MIN_CASES:", "    if False and len(cases) < SPEC_MIN_CASES:"),
 ("G2 test minimum off", "    if len(test) < SPEC_MIN_TEST:", "    if False and len(test) < SPEC_MIN_TEST:"),
 ("G3 X share per split off", "        if nx * den < len(members) * num:", "        if False and nx * den < len(members) * num:"),
 ("G4 base class minimum off", "        if by_class[cls] < SPEC_MIN_TEST_PER_BASE_CLASS:", "        if False and by_class[cls] < SPEC_MIN_TEST_PER_BASE_CLASS:"),
 ("G5 groups counted without principal check", "    if sum(1 for p in groups.values() if len(p) >= 2) < SPEC_MIN_GROUPS:", "    if len(groups) < SPEC_MIN_GROUPS:"),
 ("G6 injection minimum off", "    if len(inj) < SPEC_MIN_INJECTION:", "    if False and len(inj) < SPEC_MIN_INJECTION:"),
 ("G7 injection goals off", "        if g not in goals:", "        if False and g not in goals:"),
 ("G8 OOL minimum off", "    if len(ool) < SPEC_MIN_OOL:", "    if False and len(ool) < SPEC_MIN_OOL:"),
 ("G9 OOL X minimum off", "    if sum(1 for c in ool if c[\"class\"] == \"X\") < SPEC_MIN_OOL_X:", "    if False:"),
 ("G10 plan id check off", "    if len(by_id) != len(cases) or set(by_id) != {s[\"case_id\"] for s in plan}:", "    if False:"),
 ("G11 slot field check off", "        if c is not None and any(c[k] != s[k] for k in", "        if False and any(c[k] != s[k] for k in"),
 ("G12 singles may share family", "        if len(members) == 1 and gids == {None}:", "        if gids == {None}:"),
 ("G13 group family may be shared", "        if (len(members) == 2 and len(gids) == 1 and None not in gids", "        if (len(gids) == 1 and None not in gids"),
 ("G14 group-spans-families off", "        if len({c[\"family_id\"] for c in cases if c[\"group_id\"] == gid}) != 1:", "        if False:"),
 ("G15 X single-template share off", "        if xs[0][1] * den > total * num:", "        if False:"),
 ("G16 X top3 share off", "        if top3 * den > total * num:", "        if False:"),
 ("G17 probe share off", "    if probes * den < len(test) * num:", "    if False and probes * den < len(test) * num:"),
 ("G18 unused template off", "        if t.id not in used:\n            out.append(f\"template {t.id} binds no case\")\n        elif", "        if False:\n            out.append(f\"template {t.id} binds no case\")\n        elif t.id in used and"),
 ("G19 X template minimum off", "        elif t.cls == \"X\" and x_used.get(t.id, 0) < X_MIN_TEMPLATE_CASES:", "        elif False:"),
 ("G20 retired-in-catalog off", "        if t.id in RETIRED_PRE_FREEZE:", "        if False:"),
 ("G21 retired selected off", "    for tid in sorted(set(used) & set(RETIRED_PRE_FREEZE)):", "    for tid in []:"),
 ("G22 unknown template off", "    for tid in sorted(set(used) - {t.id for t in TEMPLATES} - set(RETIRED_PRE_FREEZE)):", "    for tid in []:"),
 ("G23 assembly does not enforce", "    if enforce_gates and gate_problems(cases, plan):", "    if False:"),
 ("G24 plan not passed to gates", "    out = _spec_minimum_problems(cases)\n    if plan is not None:", "    out = _spec_minimum_problems(cases)\n    if False:"),
 ("G25 X share threshold off-by-one", "        if xs[0][1] * den > total * num:", "        if xs[0][1] * den > total * num + den * 20:"),
 ("G26 probe share 1/20", "MIN_TEST_PROBE_SHARE = PROBE_SHARE  # of the test split", "MIN_TEST_PROBE_SHARE = (1, 20)"),
]
env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
def run():
    r = subprocess.run([".venv/bin/pytest", "-q", "-p", "no:cacheprovider", "tests/test_corpus_gates.py"], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout.strip().splitlines()[-1]
code, last = run(); print("BASELINE", code, last); assert code == 0
killed = 0
try:
    for name, a, b in M:
        s = ORIG.decode()
        if s.count(a) != 1:
            print("INVALID", name, s.count(a)); continue
        F.write_text(s.replace(a, b))
        code, last = run()
        F.write_bytes(ORIG)
        k = code != 0; killed += k
        print("KILLED " if k else "SURVIVED", name, "|", last)
finally:
    F.write_bytes(ORIG)
print(f"{killed}/{len(M)} killed")
code, last = run(); print("RESTORED", code, last)
