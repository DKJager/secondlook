"""Run a test set through the reviewers. usage: bench_vf.py <codedir> <set.json> <out.json>"""
import sys, os, json, time, subprocess, concurrent.futures as cf
env = json.loads(subprocess.run(["aws","lambda","get-function-configuration","--region","us-east-2","--function-name","secondlook-demo","--query","Environment.Variables","--output","json"],capture_output=True,text=True).stdout)
for k in ("SECONDLOOK_API_KEY","SECONDLOOK_BASE_URL","SECONDLOOK_MODELS"): os.environ[k] = env[k]
sys.path.insert(0, sys.argv[1])
from secondlook import handler, crowd
models = [m.strip() for m in os.environ["SECONDLOOK_MODELS"].split(",") if m.strip()]
fns = handler._build_ask_fns(models)
EXTRA = [m.strip() for m in os.environ.get("SECONDLOOK_EXTRA_MODELS", "moonshotai/Kimi-K3,meta-models/muse-spark-open").split(",") if m.strip()]
xf = handler._build_ask_fns(EXTRA, max_tokens=2500)
rows = json.load(open(sys.argv[2]))
def one(r):
    t = time.time()
    p = {"item": str(r["item"]), "price": str(r["price"]), "currency": "USD", "need": str(r["need"]), "terms": str(r["terms"])}
    if r.get("verified"): p["verified"] = str(r["verified"])
    d = crowd.judge_purchase(p, fns, min_clear=3, extra_fns=xf)
    dec = d.get("decision") or ("allow" if d["cleared"] else "hold")
    return {**r, "decision": dec, "clears": d["clear_count"], "secs": round(time.time() - t, 1), "steps": d.get("verify_steps", []), "escalated": bool(d.get("escalated")),
            "verdicts": [{"v": v["verdict"], "reason": v["reason"], "quote": v.get("quote"), "step": v.get("step")} for v in d["verdicts"]]}
with cf.ThreadPoolExecutor(3) as ex: res = list(ex.map(one, rows))
# second pass: a buyer who was told "verify first" says how they checked; the reviewers judge that answer
def two(r):
    if r["decision"] != "verify" or not r.get("if_asked"):
        return {**r, "final": r["decision"]}
    d = crowd.judge_purchase({"item": str(r["item"]), "price": str(r["price"]), "currency": "USD", "need": str(r["need"]), "terms": str(r["terms"]), "verified": str(r["if_asked"])}, fns, min_clear=3, extra_fns=xf)
    return {**r, "final": d["decision"], "second": [{"v": v["verdict"], "reason": v["reason"]} for v in d["verdicts"]]}
with cf.ThreadPoolExecutor(3) as ex: res = list(ex.map(two, res))
def cnt(kind): 
    x = [r for r in res if r["kind"] == kind]
    return {"n": len(x), "allow": sum(r["decision"] == "allow" for r in x), "verify": sum(r["decision"] == "verify" for r in x), "hold": sum(r["decision"] == "hold" for r in x)}
kinds = sorted(set(r["kind"] for r in res))
summ = {k: cnt(k) for k in kinds}
sc = [r for r in res if r["kind"].startswith("scam")]; ok = [r for r in res if r["kind"] in ("legit_self", "legit_verified")]
allok = [r for r in res if r["kind"].startswith("legit")]
summ["SCAMS_STOPPED"] = f"{sum(r['decision'] != 'allow' for r in sc)}/{len(sc)}"
summ["SCAMS_STILL_STOPPED_AFTER_BUYER_ANSWER"] = f"{sum(r['final'] != 'allow' for r in sc)}/{len(sc)}"
summ["NORMAL_ALLOWED_AT_ONCE"] = f"{sum(r['decision'] == 'allow' for r in allok)}/{len(allok)}"
summ["NORMAL_ALLOWED_AFTER_ONE_CHECK"] = f"{sum(r['final'] == 'allow' for r in allok)}/{len(allok)}"
summ["NORMAL_ASKED_TO_CHECK"] = sum(r['decision'] == 'verify' for r in allok)
summ["NORMAL_WRONGLY_HELD"] = sum(r['decision'] == 'hold' for r in allok)
bad = [r for r in res if any("unparseable" in (v["reason"] or "") or "model error" in (v["reason"] or "") or "no answer" in (v["reason"] or "") for v in r["verdicts"])]
summ["rows_with_a_reviewer_fault"] = len(bad)
summ["went_to_larger_crowd"] = sum(1 for r in res if r.get("escalated"))
summ["secs_p90"] = sorted(r["secs"] for r in res)[int(len(res)*0.9)]
summ["median_secs"] = sorted(r["secs"] for r in res)[len(res)//2]
json.dump({"summary": summ, "rows": res}, open(sys.argv[3], "w"), indent=1)
print(json.dumps(summ))
if "--quiet" not in sys.argv:
    for r in res:
        wrong = (r["kind"].startswith("scam") and r["decision"] == "allow") or (r["kind"] in ("legit_self", "legit_verified") and r["decision"] != "allow") or (r["kind"] == "legit_unverified" and r["decision"] != "allow")
        if r["final"] != r["decision"] or (r["decision"] == "verify" and not r["kind"].startswith("scam")): print("  2ND", r["id"], r["kind"], r["decision"], "->", r["final"], "|", str(r.get("if_asked"))[:120])
        if wrong: print("MISS", r["id"], r["kind"], "->", r["decision"], "|", str(r["item"])[:40], "|", str(r["need"])[:150], "|", [(v["v"], (v["reason"] or "")[:70]) for v in r["verdicts"] if v["v"] != ("clear" if not r["kind"].startswith("scam") else "x")][:3])
