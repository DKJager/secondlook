"""Run the 61 blind purchases (written by a different AI model, never used for tuning) against the live demo.

Usage: python bench/run_blind_61_live.py [base_url]
Writes bench/results/blind_61_live_latest.json and prints a summary with every miss.
"""
import concurrent.futures as cf
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://aq6ru66fbvzlo3usums757vbxy0qlaav.lambda-url.us-east-2.on.aws").rstrip("/")
ROWS = json.load(open(os.path.join(HERE, "blind_61_scenarios.json")))


def one(row):
    body = json.dumps({"purchase": {"item": str(row["item"]), "price": str(row["price"]), "currency": "USD",
                                    "need": str(row["need"]), "terms": str(row["terms"])}}).encode()
    req = urllib.request.Request(BASE + "/agent/check", data=body, headers={"content-type": "application/json"})
    out = json.loads(urllib.request.urlopen(req, timeout=120).read())
    return {**row, "decision": out["decision"], "clear_count": out["clear_count"], "reviews": out["reviews"]}


with cf.ThreadPoolExecutor(3) as ex:
    res = list(ex.map(one, ROWS))
scams = [r for r in res if r["label"] == "scam"]
normal = [r for r in res if r["label"] == "legit"]
summary = {"scams_stopped": sum(r["decision"] == "hold" for r in scams), "scams": len(scams),
           "normal_approved": sum(r["decision"] == "allow" for r in normal), "normal": len(normal)}
json.dump({"summary": summary, "rows": res}, open(os.path.join(HERE, "results", "blind_61_live_latest.json"), "w"), indent=1)
print(json.dumps(summary))
for r in res:
    if (r["label"] == "scam") == (r["decision"] == "allow"):
        print("MISS", r["label"], "|", r["item"], "|", r["need"][:100])
