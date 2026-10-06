import subprocess, sys, shutil, os
F='src/eeb/cases/validate.py'; O=sys.argv[1]
M=[("M1 ignore remaining filters","if all(str(r[d]) == val for d, val in rest)]","if True or all(str(r[d]) == val for d, val in rest)]"),
("M2 values key omits filters","key = (pid, metric, fsub, gb)","key = (pid, metric, gb)"),
("M3 values key omits principal","key = (pid, metric, fsub, gb)","key = (metric, fsub, gb)"),
("M4 empty po_count dropped","if not members and metric != \"po_count\":\n                    continue\n                n = _num","if not members:\n                    continue\n                n = _num"),
("M5 index key omits dimension","key = (pid, name, dim)","key = (pid, name)"),
("M6 subset key omits principal","key = (pid, name, fsub)\n","key = (name, fsub)\n"),
("M7 tolerance strict","if any(abs(n - want) <= tol for n in","if any(abs(n - want) < tol for n in"),
("M8 grouping capped at one dim","for g in range(0, 3):\n                        for gb in itertools.combinations(m[\"dimensions\"], g):\n                            if any","for g in range(0, 2):\n                        for gb in itertools.combinations(m[\"dimensions\"], g):\n                            if any"),
("M9 rows cache omits principal","key = (view.pid, name)\n        if key not in self._rows","key = (name,)\n        if key not in self._rows"),
]
src=open(O).read()
env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
for name,a,b in M:
    if src.count(a)!=1: print(name,"INVALID (find count",src.count(a),")"); continue
    open(F,'w').write(src.replace(a,b))
    r=subprocess.run([".venv/bin/python","-B","-m","pytest","tests/test_case_validators.py","-q","-x","-p","no:cacheprovider"],capture_output=True,text=True,env=env)
    tail=[l for l in r.stdout.splitlines() if l.startswith("FAILED") or "passed" in l or "error" in l.lower()][-1:]
    print(name, "KILLED" if r.returncode==1 and any("FAILED" in t for t in tail) else "SURVIVED/OTHER rc=%d"%r.returncode, tail)
shutil.copy(O,F)
