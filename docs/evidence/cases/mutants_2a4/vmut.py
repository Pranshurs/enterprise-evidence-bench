"""Single mutants of the case validators, builder and gold SQL check (mutation-testing utility).

Every mutant is applied to the working tree, the case test suites are run with Postgres
required, and the file is restored from in-memory bytes. A mutant whose FIND string is not
unique is INVALID (not counted as killed)."""
import os, re, subprocess, sys
from pathlib import Path
D = Path("src/eeb/cases")
old = Path(sys.argv[1]).read_text()
OLD = eval(old[old.index("M=[") + 2: old.index("]\nenv=") + 1])
old1 = Path(sys.argv[2]).read_text()
OLD1 = eval(old1[old1.index("M=[") + 2: old1.index("]\nsrc=") + 1])
M = [(n, f, a, b) for n, f, a, b in OLD]
M += [(n, "validate.py", a, b) for n, a, b in OLD1 if not n.startswith(("M7", "M8"))]
M += [
 ("M7r tolerance strict (upper)", "validate.py", "if i < len(values) and values[i] <= want + tol:", "if i < len(values) and values[i] < want + tol:"),
 ("M7s tolerance strict (lower)", "validate.py", "i = bisect.bisect_left(values, want - tol)", "i = bisect.bisect_right(values, want - tol)"),
 ("M8r grouping capped at one dim", "validate.py", "n for g in range(0, 3) for gb in itertools.combinations", "n for g in range(0, 2) for gb in itertools.combinations"),
 ("M10 sorted cache omits filter subset", "validate.py", "key = (pid, metric, fsub)\n        if key not in self._sorted", "key = (pid, metric)\n        if key not in self._sorted"),
 ("M11 sorted cache omits principal", "validate.py", "key = (pid, metric, fsub)\n        if key not in self._sorted", "key = (metric, fsub)\n        if key not in self._sorted"),
 ("N1 probe requirement ignored", "build.py", "if need_probe and not probe:", "if False and need_probe and not probe:"),
 ("N2 probe walk shares the ordinary cursor", "build.py", 'ck = f"{t.id}|{inj}|{probe}"', 'ck = f"{t.id}|{inj}"'),
 ("N3 probe slots never requested", "build.py", "cases[slot[\"case_id\"]] = self.fill_single(slot, i, want)", "cases[slot[\"case_id\"]] = self.fill_single(slot, i, False)"),
 ("N4 probe share floored", "build.py", "need = -(-len(members) * num // den)", "need = len(members) * num // den"),
 ("N5 injection slots eligible for probes", "build.py", 'if slot["group_id"] or slot["overlays"]["injection"]:', 'if slot["group_id"]:'),
 ("N6 group slots eligible for probes", "build.py", 'if slot["group_id"] or slot["overlays"]["injection"]:', 'if slot["overlays"]["injection"]:'),
 ("N7 least-used ignores counts", "build.py", "return sorted(templates, key=lambda t: (self.template_use[t.id],", "return sorted(templates, key=lambda t: (0,"),
 ("N8 single fill does not count use", "build.py", "                self.families.add(fam)\n                self.template_use[t.id] += 1\n                case = self._case(", "                self.families.add(fam)\n                case = self._case("),
 ("N9 group fill does not count use", "build.py", "                    self.families.add(fam)\n                    self.template_use[t.id] += 1\n", "                    self.families.add(fam)\n"),
 ("N10 depends_on not followed", "validate.py", '        stack.extend(f.get("depends_on", []))\n', ""),
 ("N11 row index shared across principals", "view.py", "key = (self.pid, table, column)", "key = (\"*\", table, column)"),
 ("N12 row index drops repeated keys", "view.py", "idx.setdefault(r[column], []).append(r)", "idx[r[column]] = [r]"),
 ("N13 off_contract filter missing", "validate.py", '"po_id": ["po_id"], "off_contract": ["off_contract"],', '"po_id": ["po_id"],'),
 ("N14 depends_on not recorded", "facts.py", "    if depends_on:\n        f[\"depends_on\"] = depends_on\n", ""),
]
env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "EEB_REQUIRE_PG": "1"}
TESTS = ["tests/test_case_build.py", "tests/test_case_validators.py"]
def run():
    r = subprocess.run([".venv/bin/python", "-B", "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider"],
                       capture_output=True, text=True, env=env)
    failed = [l.split("::")[1].split(" ")[0] for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
    return r.returncode, failed, (r.stdout.strip().splitlines() or [""])[-1]
src = {f: (D / f).read_bytes() for f in {m[1] for m in M}}
if "--dry" in sys.argv:
    for n, f, a, b in M:
        c = src[f].decode().count(a)
        if c != 1: print("INVALID", n, c)
    print(len(M), "mutants"); sys.exit()
rc, failed, last = run(); print("BASELINE", rc, last, flush=True); assert rc == 0
res = {}
try:
    for n, f, a, b in M:
        s = src[f].decode()
        if s.count(a) != 1:
            print("INVALID", n, flush=True); res[n] = "INVALID"; continue
        (D / f).write_text(s.replace(a, b))
        rc, failed, last = run()
        (D / f).write_bytes(src[f])
        st = "KILLED" if rc == 1 and failed else "SURVIVED" if rc == 0 else f"OTHER rc={rc}"
        res[n] = st; print(st, n, failed[:3], "|", last, flush=True)
finally:
    for f, b in src.items(): (D / f).write_bytes(b)
from collections import Counter
print(dict(Counter(res.values())))
rc, failed, last = run(); print("RESTORED", rc, last)
