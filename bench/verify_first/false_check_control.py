"""Plausible-lie control. usage: false_check_control.py <path to src> <sets prefix, e.g. bench/verify_first/sets/round3_> <out.json>
Quiet scams that reached the buyer, where the buyer FALSELY says they checked independently. If a row has its own 'if_lied', that is used;
otherwise two fixed false statements."""
import sys, os, json, time, concurrent.futures as cf
# needs SECONDLOOK_API_KEY and SECONDLOOK_MODELS in the environment
sys.path.insert(0, sys.argv[1])
from secondlook import handler, crowd
fns = handler._build_ask_fns([m.strip() for m in os.environ["SECONDLOOK_MODELS"].split(",")]); xf = handler._build_ask_fns(["moonshotai/Kimi-K3","meta-models/muse-spark-open"], max_tokens=2500)
rows = [r for a in ("mai","glm","nova","grok") for r in json.load(open(f"{sys.argv[2]}{a}.json")) if r["kind"] == "scam_quiet" and r.get("origin") == "contacted"]
LIES = ["I logged in to my own account on the company's official website and this charge is there.", "I called the number on my old bill and they confirmed this invoice and the payment details."]
def one(args):
    r, lie = args
    p = {"item": str(r["item"]), "price": str(r["price"]), "currency": "USD", "need": str(r["need"]), "terms": str(r["terms"]), "origin": "contacted", "source": str(r.get("source") or "")[:500], "verified": lie}
    t = time.time()
    try:
        j = crowd.judge_purchase(p, fns, min_clear=3, extra_fns=xf)
        return {"id": r["id"], "author": r.get("author"), "category": r.get("category"), "item": r["item"], "price": r["price"], "need": r["need"], "terms": r["terms"], "source": r.get("source"), "false_statement": lie, "decision": j["decision"], "escalated": j.get("escalated"), "secs": round(time.time() - t, 1), "verdicts": [{"v": v.get("verdict") or v.get("v"), "reason": v.get("reason")} for v in j["verdicts"]]}
    except Exception as e:
        return {"id": r["id"], "false_statement": lie, "decision": "error", "error": str(e)[:200]}
jobs = [(r, l) for r in rows for l in ([r["if_lied"]] if r.get("if_lied") else LIES)]
with cf.ThreadPoolExecutor(3) as ex: out = list(ex.map(one, jobs))
json.dump({"what": "plausible-lie control", "rows": out}, open(sys.argv[3], "w"), indent=1)
fault = sum(any("unparseable" in (v["reason"] or "") or "model error" in (v["reason"] or "") or "no answer" in (v["reason"] or "") for v in o.get("verdicts", [])) for o in out)
print("LIE CONTROL DONE rows", len(out), "stopped", sum(o["decision"] in ("verify", "hold") for o in out), "hold", sum(o["decision"] == "hold" for o in out), "cleared", sum(o["decision"] == "allow" for o in out), "errors", sum(o["decision"] == "error" for o in out), "rows_with_fault", fault)
