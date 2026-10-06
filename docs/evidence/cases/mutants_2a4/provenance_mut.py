"""Single mutants of the freeze's paraphrase-provenance binding. Mutation-testing utility.

Runs tests/test_freeze.py after an unmutated baseline passes; restores from saved bytes."""
import os, subprocess
from pathlib import Path
M = [
 ("P1 freeze ignores provenance", "src/eeb/cases/freeze.py", '    if bad:\n        raise FreezeError("provenance: "', '    if False:\n        raise FreezeError("provenance: "'),
 ("P2 missing provenance accepted", "src/eeb/cases/paraphrase.py", '    if runs is None:\n        return ["rephrase provenance is missing"]', '    if runs is None:\n        return []'),
 ("P3 text not bound", "src/eeb/cases/paraphrase.py", '               canonical.sha256_text(str(e["rephrased_question"])))\n        if key not in bound:', '               canonical.sha256_text(str(e["rephrased_question"])))\n        if key[0] not in {b[0] for b in bound}:'),
 ("P4 model not checked", "src/eeb/cases/paraphrase.py", '                if p["model"] != f"{r[\'family\']}:{r[\'model\']}":', '                if False:'),
 ("P5 malformed run accepted", "src/eeb/cases/paraphrase.py", '            out.append(f"rephrase provenance run {i} is malformed")\n            continue', '            continue'),
]
env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
def run():
    r = subprocess.run([".venv/bin/python", "-B", "-m", "pytest", "tests/test_freeze.py", "-q", "-p", "no:cacheprovider"], capture_output=True, text=True, env=env)
    failed = [l.split("::")[1].split(" ")[0] for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
    return r.returncode, failed, (r.stdout.strip().splitlines() or [""])[-1]
src = {f: Path(f).read_bytes() for f in {m[1] for m in M}}
rc, _, last = run(); print("BASELINE", rc, last, flush=True); assert rc == 0
res = []
try:
    for n, f, a, b in M:
        s = src[f].decode()
        if s.count(a) != 1:
            print("INVALID", n); res.append("INVALID"); continue
        Path(f).write_text(s.replace(a, b))
        rc, failed, last = run()
        Path(f).write_bytes(src[f])
        st = "KILLED" if rc == 1 and failed else "SURVIVED" if rc == 0 else f"OTHER rc={rc}"
        res.append(st); print(st, n, failed[:2], "|", last, flush=True)
finally:
    for f, b in src.items(): Path(f).write_bytes(b)
print({k: res.count(k) for k in set(res)})
rc, _, last = run(); print("RESTORED", rc, last)
