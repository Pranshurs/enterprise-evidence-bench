"""Single mutants of the injection-carrier split rule (F-17). Mutation-testing utility.

Runs tests/test_corpus_gates.py and tests/test_case_build.py (Postgres required) per mutant,
after an unmutated baseline passes; restores each file from saved bytes."""
import os, subprocess
from pathlib import Path
D = Path("src/eeb/cases")
M = [
 ("C1 carrier gate off", "corpus.py", "        if len(sp) > 1:\n            out.append(f\"injection carrier", "        if False:\n            out.append(f\"injection carrier"),
 ("C2 per-split goal gate off", "corpus.py", "            if g not in goals:\n                out.append(f\"{split}: no injection", "            if False:\n                out.append(f\"{split}: no injection"),
 ("C3 carrier gate not called", "corpus.py", "    out += _carrier_problems(cases)\n", ""),
 ("B1 builder ignores carrier split", "build.py", "if got is None or self.carrier_split.get(got[1][\"incident_id\"]) != split:", "if got is None:"),
 ("B2 candidate cache ignores split", "build.py", "key = f\"{t.id}|{injection}|{split if injection else None}\"", "key = f\"{t.id}|{injection}\""),
 ("B3 cursor ignores split", "build.py", "ck = f\"{t.id}|{inj}|{probe}\" + (f\"|{slot['split']}\" if inj else \"\")", "ck = f\"{t.id}|{inj}|{probe}\""),
 ("B4 no carrier of every goal per split", "build.py", "while quota[sp] == 0 and len(ids) >= len(splits):", "while False:"),
 ("B5 largest remainder dropped", "build.py", "for sp in rest[:len(ids) - sum(quota.values())]:", "for sp in rest[:0]:"),
 ("B6 order from input, not seed", "build.py", "                     key=lambda i: _rank(seed, \"carrier\", i))", "                     key=lambda i: 0)"),
 ("B7 unreadable carriers allocated", "build.py", "if c[\"goal\"] == goal and c[\"incident_id\"] in readable),", "if c[\"goal\"] == goal),"),
]
env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "EEB_REQUIRE_PG": "1"}
TESTS = ["tests/test_corpus_gates.py", "tests/test_case_build.py"]
def run():
    r = subprocess.run([".venv/bin/python", "-B", "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider"], capture_output=True, text=True, env=env)
    failed = [l.split("::")[1].split(" ")[0] for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
    return r.returncode, failed, (r.stdout.strip().splitlines() or [""])[-1]
src = {f: (D / f).read_bytes() for f in {m[1] for m in M}}
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
