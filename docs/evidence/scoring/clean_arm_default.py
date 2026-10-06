"""Clean arm over the full default-scale corpus (offline receipts): every gold-perfect
response must score perfectly. Usage: python clean_arm_default.py <cases.jsonl>"""
import json, time, collections, sys
from pathlib import Path
from eeb.cases.view import InstanceData
from eeb.scoring.evidence import Evidence
from eeb.scoring.ideal import ideal_response, sql_facts_cited
from eeb.scoring.score import Observed, ReceiptVerdict, score_case
t=time.time()
data=InstanceData.load(Path("instances/seed7-default")); ev=Evidence(data)
cases=[json.loads(l) for l in open(sys.argv[1])]
print("load", round(time.time()-t,1))
bad=collections.Counter(); ex={}
for c in cases:
    r=ideal_response(c)
    obs=Observed(receipts={rid: ReceiptVerdict("verified", ["value"], [{"value": f["value"]}]) for rid,f in sql_facts_cited(r,c).items()})
    s=score_case(c, r, ev, obs)
    probs=[]
    if s["outcome"]!=s["gold_outcome"]: probs.append("outcome")
    f=s["facts"]
    if f["correct"]!=f["required"]: probs.append("facts")
    if f["wrong"]: probs.append("wrong")
    ci=s["citations"]
    if ci["valid"]!=ci["total"]: probs.append("cit_invalid")
    if ci["unsupported"]: probs.append("unsupported")
    if ci["required_satisfied"]!=ci["required"]: probs.append("req_cit")
    if s["sources"]["satisfied"] is False: probs.append("sources")
    if s["sql"]["execution_correct"]!=s["sql"]["gold_sql_facts"]: probs.append("sql_exec")
    if s["conflicts"]["disclosed"]!=s["conflicts"]["expected"]: probs.append("conflict")
    if s["staleness"]["stale"]: probs.append("stale")
    if s["clarify"] and not all(s["clarify"].values()): probs.append("clarify")
    if s["injection"] and s["injection"]["success"]: probs.append("inj_"+s["injection"]["goal"])
    if s["leaks"]["probe_value_returned"]: probs.append("probe")
    for p in probs:
        bad[(p, c["template_id"])]+=1; ex.setdefault((p,c["template_id"]), (c["case_id"], s))
print("secs", round(time.time()-t,1), "cases", len(cases), "bad", sum(bad.values()))
for k,v in sorted(bad.items()): print(k, v)
for k,(cid,s) in list(ex.items())[:6]:
    print("---",k,cid, json.dumps({x:s[x] for x in ("facts","citations","sql","conflicts","injection")}, default=str)[:900])
