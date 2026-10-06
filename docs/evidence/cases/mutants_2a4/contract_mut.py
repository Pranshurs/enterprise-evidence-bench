"""Single mutants of the gold-contract rules (F-18, F-19). Mutation-testing utility.

Runs tests/test_corpus_gates.py and tests/test_case_build.py (Postgres required) per mutant,
after an unmutated baseline passes; restores each file from saved bytes."""
import os, subprocess
from pathlib import Path
D = Path("src/eeb/cases")
M = [
 ("G1 money currency not required at build", "facts.py", '    if kind == "money" and unit not in CURRENCIES:', '    if False:'),
 ("G2 contract error is a ValueError", "facts.py", 'class GoldContractError(Exception):', 'class GoldContractError(ValueError):'),
 ("G3 kinds from direct inputs (F-19 reintroduced)", "templates.py", '        if f["source"] == "derived":\n            stack.extend(f["derived"]["inputs"])\n        else:\n            kinds.add(f["source"])', '        if f["source"] == "derived" and f["fact_id"] == fid:\n            stack.extend(f["derived"]["inputs"])\n        elif f["source"] != "derived":\n            kinds.add(f["source"])'),
 ("G4 declared kinds not compared", "templates.py", '        if sorted(kinds) != citations.get(fid):', '        if False:'),
 ("G5 corpus gate: money currency off", "corpus.py", '            if f["kind"] == "money" and f["unit"] not in ("INR", "EUR", "GBP"):', '            if False:'),
 ("G6 corpus gate: kinds off", "corpus.py", '            if required.get(fid) != sorted(sources):', '            if False:'),
 ("G7 corpus gate not called", "corpus.py", '    out += _gold_contract_problems(cases)\n', ''),
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
