import subprocess, sys, shutil, os
O=sys.argv[1]; D='src/eeb/cases/'
M=[
("V1 invisible chunk not reported","validate.py",'''elif f["source"] == "doc" and not view.chunk_visible(''','''elif f["source"] == "doc" and False and not view.chunk_visible('''),
("V2 ungranted column not reported","validate.py",'''                if not view.can_read(t, cols):
                    missing.append''','''                if False and not view.can_read(t, cols):
                    missing.append'''),
("V3 doc value in DB row never found","validate.py",'''            if want is not None and n is not None and n == want:
                return True''','''            if want is not None and n is not None and n == want:
                return False'''),
("V4 cited documents not searched","validate.py",'''            if doc_id in cited or any(k in d["rendered"] for k in keys)]''','''            if any(k in d["rendered"] for k in keys)]'''),
("V4b supplier's contracts not searched","validate.py",'''    if "supplier_id" in slots:
        keys |= {c["contract_id"] for c in data.tables["contracts"]
                 if c["supplier_id"] == slots["supplier_id"]}''','''    if False and "supplier_id" in slots:
        keys |= {c["contract_id"] for c in data.tables["contracts"]
                 if c["supplier_id"] == slots["supplier_id"]}'''),
("V5 SQL value in documents never found","validate.py",'''            if Decimal(m.group(0).replace(",", "")) == want:
                return True''','''            if Decimal(m.group(0).replace(",", "")) == want:
                return False'''),
("V6 necessity rejection off","build.py",'''if cls == "X" and nec and (''','''if cls == "Z" and nec and ('''),
("V7 OOL-but-reconstructible accepted","build.py",'''            if want_ool and mem["in_metric_layer"]:''','''            if False and want_ool and mem["in_metric_layer"]:'''),
("V8 in-layer-but-unreconstructible accepted","build.py",'''            if not want_ool and not mem["in_metric_layer"]:''','''            if False and not want_ool and not mem["in_metric_layer"]:'''),
("V9 principal lacking evidence accepted","build.py",'''if gold["expected_outcome"] == "ANSWER" and miss:''','''if gold["expected_outcome"] == "ANSWER" and miss and False:'''),
("V10 family rule off for singles","build.py",'''if key in self.used or fam in self.families:''','''if key in self.used:'''),
("V11 restricted probe empty","validate.py",'''        if f["source"] == "sql" and f["fact_id"] in g and g[f["fact_id"]]["value"] != f["value"]:''','''        if False and f["source"] == "sql" and f["fact_id"] in g and g[f["fact_id"]]["value"] != f["value"]:'''),
("V12 principal value not compared","dbcheck.py",'''                    if not ok:
                        problems.append({**where, "problem": "principal_value_mismatch",''','''                    if not ok and False:
                        problems.append({**where, "problem": "principal_value_mismatch",'''),
("V13 gold SQL run as administrator","dbcheck.py",'''rows = conns[pid].execute(f["gold_sql"]).fetchall()''','''rows = root.execute(f["gold_sql"]).fetchall()'''),
("V14 unrestricted probe not compared","dbcheck.py",'''                        if not ok:
                            problems.append({**where, "problem": "unrestricted_value_mismatch",''','''                        if not ok and False:
                            problems.append({**where, "problem": "unrestricted_value_mismatch",'''),
("V15 permitted member may have partial view","build.py",'''if v is None or v["restricted_probe"]:''','''if v is None:'''),
("V16 denial not proven","build.py",'''            if missing:
                return pid, missing
        return None''','''            return pid, missing or ["unproven"]
        return None'''),
("V17 group ignores used families","build.py",'''                fam = family_id(t, slots)
                if fam in self.families:
                    continue
                global_gold''','''                fam = family_id(t, slots)
                global_gold'''),
("V18 SQL error swallowed","dbcheck.py",'''                        problems.append({**where, "problem": "execution_error",
                                         "sqlstate": e.sqlstate})
                        continue''','''                        continue'''),
]
env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
src={f:open(os.path.join(O,f)).read() for f in ("validate.py","build.py","dbcheck.py")}
res=[]
for name,f,a,b in M:
    if src[f].count(a)!=1: print(name,"INVALID find count",src[f].count(a)); res.append("INVALID"); continue
    open(D+f,'w').write(src[f].replace(a,b))
    r=subprocess.run([".venv/bin/python","-B","-m","pytest","tests/test_case_build.py","-q","-p","no:cacheprovider"],capture_output=True,text=True,env=env)
    failed=[l.split("::")[1].split(" ")[0] for l in r.stdout.splitlines() if l.startswith(("FAILED","ERROR"))]
    st="KILLED" if r.returncode==1 and failed else "SURVIVED" if r.returncode==0 else "OTHER rc=%d"%r.returncode
    res.append(st); print(name, st, failed[:3])
    shutil.copy(os.path.join(O,f), D+f)
print({k:res.count(k) for k in set(res)})
